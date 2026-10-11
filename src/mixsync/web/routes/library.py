from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from mixsync.acquire.merge import PlanInProgress, enqueue_merge, enqueue_undo, live_job
from mixsync.core.capabilities import Capability
from mixsync.core.errors import TransientError
from mixsync.db.auth import AuthUser
from mixsync.db.models.library import Plan, Track
from mixsync.library import fragmentation, tags
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
    if plan.status == "draft":
        return {"confirm"}
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


@router.get("/unverified")
def unverified(request: Request, user: User, db: Db) -> HTMLResponse:
    rows = db.scalars(select(Track).where(Track.status == "unverified").order_by(Track.id)).all()
    root = Path(request.app.state.settings.data_dir) / "library"
    items: list[tuple[Track, dict[str, str]]] = []
    for t in rows:
        try:
            items.append((t, tags.read_fields(root / t.path)))
        except Exception:  # file moved or unreadable: the path alone still identifies it
            items.append((t, {}))
    return templates.TemplateResponse(
        request, "pages/unverified.html", {"user": user, "items": items}
    )


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


@router.post("/plans/{plan_id}/resume")
def resume(
    plan_id: int, user: User, db: Db, count: Annotated[str, Form()] = ""
) -> RedirectResponse:
    return _act(plan_id, "resume", db, count, enqueue_merge)


@router.post("/plans/{plan_id}/undo")
def undo(plan_id: int, user: User, db: Db, count: Annotated[str, Form()] = "") -> RedirectResponse:
    return _act(plan_id, "undo", db, count, enqueue_undo)
