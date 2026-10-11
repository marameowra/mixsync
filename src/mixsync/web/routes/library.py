from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from mixsync.acquire import recheck
from mixsync.acquire.merge import PlanInProgress, enqueue_merge, enqueue_undo, live_job
from mixsync.acquire.requests import enqueue_recheck
from mixsync.core.capabilities import Capability
from mixsync.core.errors import TransientError
from mixsync.db.auth import AuthUser
from mixsync.db.models.library import Plan
from mixsync.library import fragmentation, tags
from mixsync.submit.harmony import harmony_url
from mixsync.submit.mb_seed import (
    SEED_URL,
    Credit,
    SeedMedium,
    SeedRelease,
    SeedTrack,
    build_seed,
)
from mixsync.web.deps import Db, Merge, require
from mixsync.web.templating import templates

router = APIRouter(prefix="/library")
User = Annotated[AuthUser, Depends(require(Capability.library_edit))]

_TYPED_ABOVE = 50  # data-safety rule 5: more file changes than this need the count typed
templates.env.globals["typed_above"] = _TYPED_ABOVE  # pyright: ignore[reportUnknownMemberType]


def _changes(plan: Plan) -> int:
    return sum(op["op"] in ("move", "trash") for op in plan.ops)


def _plan(db: Db, plan_id: int) -> Plan:
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(404, "no such plan")
    return plan


def _allowed(db: Session, plan: Plan) -> set[str]:
    """The actions this plan's state allows. Resume and undo also need no live job on the plan,
    or a second worker would race the first."""
    if plan.status == "draft":  # a confirmed draft (its job waits) can still be confirmed again
        return {"confirm"} if live_job(db, plan.id) else {"confirm", "discard"}
    if live_job(db, plan.id):
        return set()
    if plan.status == "failed" and not (plan.error or "").startswith("undo:"):
        return {"resume", "undo"}
    return {"undo"} if plan.status in ("done", "failed", "running", "undoing") else set()


def _check_count(plan: Plan, count: str) -> None:
    if _changes(plan) > _TYPED_ABOVE and count.strip() != str(_changes(plan)):
        raise HTTPException(400, f"type {_changes(plan)} to confirm")


@router.get("/fragments")
def fragments(request: Request, user: User, db: Db) -> HTMLResponse:
    plans = db.scalars(select(Plan).order_by(Plan.id.desc()).limit(20)).all()
    return templates.TemplateResponse(
        request,
        "pages/fragments.html",
        {
            "user": user,
            "items": fragmentation.find(db),
            "plans": plans,
            "allowed": {p.id: _allowed(db, p) for p in plans},
        },
    )


def _read_unverified(request: Request, db: Session) -> list[recheck.Item]:
    return recheck.read_unverified(db, Path(request.app.state.settings.data_dir) / "library")


@router.get("/unverified")
def unverified(request: Request, user: User, db: Db) -> HTMLResponse:
    items = _read_unverified(request, db)
    albums = recheck.group(items)
    ctx = {
        "user": user,
        "count": len(items),
        "albums": albums,
        "status": recheck.statuses(db, albums),
    }
    return templates.TemplateResponse(request, "pages/unverified.html", ctx)


@router.post("/unverified/check")
def check_now(
    request: Request,
    user: User,
    db: Db,
    album: Annotated[str, Form()],
    albumartist: Annotated[str, Form()] = "",
    year: Annotated[str, Form()] = "",
) -> RedirectResponse:
    key = (albumartist, album, year)
    if key not in recheck.group(_read_unverified(request, db)):
        raise HTTPException(404, "no unverified tracks for that album")
    enqueue_recheck(db, datetime.now(UTC), key, user.id)
    return RedirectResponse("/library/unverified", status_code=303)


def _num(v: str | None, default: int) -> int:
    return int(v) if v and v.isascii() and v.isdigit() else default


def _seed_release(
    request: Request, items: list[recheck.Item], album: str, artist: str, year: str
) -> SeedRelease:
    root = Path(request.app.state.settings.data_dir) / "library"
    discs: dict[int, list[recheck.Item]] = {}
    for it in items:
        discs.setdefault(_num(it[1].get("discnumber"), 1), []).append(it)
    mediums: list[SeedMedium] = []
    for d in sorted(discs):
        disc = sorted(discs[d], key=lambda i: (_num(i[1].get("tracknumber"), 10**6), i[0].path))
        nums = [_num(tg.get("tracknumber"), 0) for _, tg in disc]
        nxt = max(nums)  # unnumbered tracks continue after the highest number (1.. if none)
        out: list[SeedTrack] = []
        for t, tg in disc:
            try:
                ms = tags.length_ms(root / t.path)
            except Exception:  # unreadable audio: seed without a length
                ms = None
            if not tg.get("tracknumber"):
                nxt += 1
            a = tg.get("artist", "")
            out.append(
                SeedTrack(
                    tg.get("tracknumber") or str(nxt),
                    tg.get("title") or Path(t.path).stem,
                    ms,
                    [Credit(a)] if a and a != artist else [],
                )
            )
        mediums.append(SeedMedium(out))
    return SeedRelease(
        album, [Credit(artist)] if artist else [], mediums, year=int(year) if year else None
    )


@router.get("/unverified/submit")
def unverified_submit(
    request: Request,
    user: User,
    db: Db,
    album: str,
    albumartist: str = "",
    year: str = "",
    store_url: str = "",
) -> HTMLResponse:
    items = recheck.group(_read_unverified(request, db)).get((albumartist, album, year), [])
    if not items:
        raise HTTPException(404, "no unverified tracks for that album")
    seen = Counter(
        (_num(tg.get("discnumber"), 1), _num(tg["tracknumber"], 0) or tg["tracknumber"])
        for _, tg in items
        if tg.get("tracknumber")
    )
    dupes = sorted(f"disc {d} track {n}" for (d, n), c in seen.items() if c > 1)
    harmony, error = None, None
    if store_url.strip():
        try:
            harmony = harmony_url(store_url)
        except ValueError as e:
            error = str(e)
    ctx = {
        "user": user,
        "album": album,
        "albumartist": albumartist,
        "year": year,
        "dupes": dupes,
        "store_url": store_url,
        "harmony": harmony,
        "error": error,
        "seed_url": SEED_URL,
        "seed": build_seed(_seed_release(request, items, album, albumartist, year)),
    }
    resp = templates.TemplateResponse(request, "pages/unverified_submit.html", ctx)
    resp.headers["Referrer-Policy"] = "no-referrer"  # Chromium ignores rel=noreferrer on forms
    return resp


@router.post("/fragments/plan")
async def build_plan(
    user: User, db: Db, merger: Merge, group: Annotated[str, Form()]
) -> RedirectResponse:
    db.commit()  # the login's last_seen write must not hold the SQLite lock while build() writes
    try:
        plan_id = await merger.build(group)
    except PlanInProgress as e:  # a ValueError, not a LookupError
        raise HTTPException(409, str(e)) from None
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    except (TransientError, httpx.HTTPError):
        raise HTTPException(503, "MusicBrainz is unavailable; try again later") from None
    return RedirectResponse(f"/library/plans/{plan_id}", status_code=303)


@router.get("/plans/{plan_id}")
def plan_detail(plan_id: int, request: Request, user: User, db: Db) -> HTMLResponse:
    plan = _plan(db, plan_id)
    counts = {k: sum(op["op"] == k for op in plan.ops) for k in ("move", "trash", "keep")}
    ctx = {"user": user, "plan": plan, "counts": counts, "allowed": _allowed(db, plan)}
    return templates.TemplateResponse(request, "pages/plan.html", ctx)


def _act(
    plan_id: int,
    action: str,
    db: Session,
    count: str,
    enqueue: Callable[[Session, datetime, int], None],
) -> RedirectResponse:
    plan = _plan(db, plan_id)
    if action not in _allowed(db, plan):
        raise HTTPException(409, f"plan is {plan.status}")
    _check_count(plan, count)
    enqueue(db, datetime.now(UTC), plan.id)
    return RedirectResponse(f"/library/plans/{plan_id}", status_code=303)


@router.post("/plans/{plan_id}/confirm")
def confirm(
    plan_id: int, user: User, db: Db, count: Annotated[str, Form()] = ""
) -> RedirectResponse:
    return _act(plan_id, "confirm", db, count, enqueue_merge)


@router.post("/plans/{plan_id}/discard")
def discard(plan_id: int, user: User, db: Db) -> RedirectResponse:
    plan = _plan(db, plan_id)
    if "discard" not in _allowed(db, plan):
        raise HTTPException(409, f"plan is {plan.status}")
    plan.status = "discarded"  # a preview that never ran: DB only
    return RedirectResponse(f"/library/plans/{plan_id}", status_code=303)


@router.post("/plans/{plan_id}/resume")
def resume(
    plan_id: int, user: User, db: Db, count: Annotated[str, Form()] = ""
) -> RedirectResponse:
    return _act(plan_id, "resume", db, count, enqueue_merge)


@router.post("/plans/{plan_id}/undo")
def undo(plan_id: int, user: User, db: Db, count: Annotated[str, Form()] = "") -> RedirectResponse:
    return _act(plan_id, "undo", db, count, enqueue_undo)
