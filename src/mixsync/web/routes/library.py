import hashlib
import threading
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mixsync.acquire import missing_scan, recheck
from mixsync.acquire.merge import (
    DeleteRefused,
    Merger,
    PlanInProgress,
    enqueue_merge,
    enqueue_reorganize,
    enqueue_undo,
    live_job,
    reorganize_build,
    stale_delete_draft,
)
from mixsync.acquire.requests import enqueue_recheck
from mixsync.core.capabilities import Capability
from mixsync.core.errors import TransientError
from mixsync.core.jobs import LIVE
from mixsync.db import progress
from mixsync.db.auth import AuthUser
from mixsync.db.models.library import Plan, Track
from mixsync.db.models.safety import FileOp, TagSnapshot
from mixsync.db.models.work import Request as AlbumRequest
from mixsync.db.undo import undoable
from mixsync.library import albums, fragmentation, layout, tag_undo, tags
from mixsync.library.fileops import FileOps
from mixsync.submit.harmony import harmony_url
from mixsync.submit.mb_seed import (
    SEED_URL,
    Credit,
    SeedMedium,
    SeedRelease,
    SeedTrack,
    build_seed,
)
from mixsync.web.csp import csp_with_form_target
from mixsync.web.deps import CurrentUser, Db, Files, Merge, Namer, require
from mixsync.web.names import MAX_LOOKUPS, resolve
from mixsync.web.routes.requests import delete_row
from mixsync.web.templating import render, templates

router = APIRouter(prefix="/library")
User = Annotated[AuthUser, Depends(require(Capability.library_edit))]

TYPED_ABOVE = 50  # data-safety rule 5: more file changes than this need the count typed
templates.env.globals["typed_above"] = TYPED_ABOVE  # pyright: ignore[reportUnknownMemberType]


def _changes(plan: Plan) -> int:
    return sum(op["op"] in ("move", "trash") for op in plan.ops)


def _plan(db: Db, plan_id: int, user: AuthUser) -> Plan:
    """The plan, if the user may act on it: `library.edit`, or the owner of the request whose
    music a delete plan removes (and nothing else)."""
    plan = db.get(Plan, plan_id)
    mine = plan is not None and any(
        op["op"] == "request" and op["user_id"] == user.id for op in plan.ops
    )
    if not (mine or Capability.library_edit in user.capabilities):
        raise HTTPException(403, f"missing capability: {Capability.library_edit.value}")
    if plan is None or plan.kind == missing_scan.KIND:  # confirmed on the settings page
        raise HTTPException(404, "no such plan")
    return plan


def _days(request: Request) -> int:
    days: int = request.app.state.settings.trash_days
    return days


def _allowed(db: Session, plan: Plan, days: int) -> set[str]:
    """The actions this plan's state allows. Resume and undo also need no live job on the plan,
    or a second worker would race the first."""
    if plan.status == "draft":  # a confirmed draft (its job waits) can still be confirmed again
        return {"confirm"} if live_job(db, plan.id) else {"confirm", "discard"}
    if live_job(db, plan.id):
        return set()
    if plan.status == "failed" and not (plan.error or "").startswith(("undo:", "blocked:")):
        return {"resume", "undo"} if undoable(plan, datetime.now(UTC), days) else {"resume"}
    return {"undo"} if undoable(plan, datetime.now(UTC), days) else set()


def _check_count(plan: Plan, count: str) -> None:
    if _changes(plan) > TYPED_ABOVE and count.strip() != str(_changes(plan)):
        raise HTTPException(400, f"type {_changes(plan)} to confirm")


@router.get("/fragments")
async def fragments(request: Request, user: User, db: Db, namer: Namer) -> HTMLResponse:
    stuck = Plan.status.in_(("running", "undoing", "failed"))  # these block their release group
    ours = select(Plan).where(Plan.kind != missing_scan.KIND)  # a scan draft moves no file
    plans = [
        *db.scalars(ours.where(stuck).order_by(Plan.id.desc())),
        *db.scalars(ours.where(~stuck).order_by(Plan.id.desc()).limit(20)),
    ]
    plans.sort(key=lambda p: -p.id)
    items = fragmentation.find(db)
    refs = {("release-group", p.release_group_mbid) for p in plans}
    for f in items:  # list page: local rows and the cache only, no MusicBrainz call
        refs |= {("release-group", f.group_mbid), *(("release", r) for r in f.releases)}
        refs |= {("release", f.canonical)} if f.canonical else set()
    return templates.TemplateResponse(
        request,
        "pages/fragments.html",
        {
            "user": user,
            "items": items,
            "plans": plans,
            "names": await resolve(db, namer, refs),
            "allowed": {p.id: _allowed(db, p, _days(request)) for p in plans},
        },
    )


@router.get("/unverified")
def unverified(request: Request, user: User, db: Db) -> HTMLResponse:
    items = recheck.read_unverified(db)
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
    if key not in recheck.group(recheck.read_unverified(db)):
        raise HTTPException(404, "no unverified tracks for that album")
    enqueue_recheck(db, datetime.now(UTC), key, user.id)
    return RedirectResponse("/library/unverified", status_code=303)


def _num(v: str | None, default: int) -> int:
    return int(v) if v and v.isascii() and v.isdigit() else default


_Item = tuple[Track, dict[str, str]]


def _seed_release(
    request: Request, items: list[_Item], album: str, artist: str, year: str
) -> SeedRelease:
    root = request.app.state.services.fileops.library
    discs: dict[int, list[_Item]] = {}
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
    rows = recheck.group(recheck.read_unverified(db)).get((albumartist, album, year), [])
    if not rows:
        raise HTTPException(404, "no unverified tracks for that album")
    items: list[_Item] = []
    for t in rows:  # one album's files, for the seed's disc and track numbers
        try:
            items.append((t, tags.read_fields(request.app.state.services.fileops.library / t.path)))
        except Exception:  # file moved or unreadable: the path alone still identifies it
            items.append((t, {}))
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
    resp.headers["Content-Security-Policy"] = csp_with_form_target(SEED_URL)  # form posts to MB
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


@router.post("/reorganize/plan")
def build_reorganize(user: User, db: Db) -> RedirectResponse:
    """Queue the build: it reads the tags of every drifted file, too slow for a request."""
    enqueue_reorganize(db, datetime.now(UTC))
    return RedirectResponse("/library/reorganize", status_code=303)


@router.get("/reorganize")
def reorganize_status(request: Request, user: User, db: Db) -> HTMLResponse:
    """A live region (ADR 0017): building, then a link to the draft or why it failed."""
    job, plan_id = reorganize_build(db)
    ctx = {
        "user": user,
        "job": job,
        "plan_id": plan_id,
        "building": job is not None and job.status in LIVE,
    }
    return render(request, "pages/reorganize.html", "pages/reorganize_live.html", ctx)


SAMPLE, FOLDERS = 3, 100  # a reorganize page shows this many moves per folder, of this many


def _by_folder(ops: list[dict[str, Any]]) -> dict[str, Any]:
    """Moves and refusals grouped by the album folder they leave: (folder, count, sample) for
    the first FOLDERS folders, and how many folders are left out."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for op in ops:
        if op["op"] in ("move", "refused"):
            groups.setdefault(op["old"]["path"].rpartition("/")[0], []).append(op)
    shown = [(f, len(o), o[:SAMPLE]) for f, o in list(groups.items())[:FOLDERS]]
    return {"folders": shown, "folders_hidden": len(groups) - len(shown)}


@router.get("/plans/{plan_id}")
async def plan_detail(
    plan_id: int, request: Request, user: CurrentUser, db: Db, namer: Namer
) -> HTMLResponse:
    plan = _plan(db, plan_id, user)
    kinds = ("move", "trash", "keep", "refused")
    counts = {k: sum(op["op"] == k for op in plan.ops) for k in kinds}
    refs = {("release-group", plan.release_group_mbid), ("release", plan.canonical_release_mbid)}
    for op in plan.ops:
        if op["op"] == "move" and "tags" in op:
            refs |= {
                ("release", op["old"].get("release_mbid") or ""),
                ("release", op["tags"].get("release_id") or ""),
            }
    allowed = _allowed(db, plan, _days(request))  # all DB reads first: resolve() commits
    ops = progress.with_done(db, plan)
    ctx = {
        "user": user,
        "plan": plan,
        "ops": ops,
        "counts": counts,
        "allowed": allowed,
        "names": await resolve(db, namer, refs, MAX_LOOKUPS),
        **(_by_folder(ops) if plan.kind == "reorganize" else {}),
    }
    return templates.TemplateResponse(request, "pages/plan.html", ctx)


MOVES_PAGE = 200


@router.get("/plans/{plan_id}/moves")
def plan_moves(
    plan_id: int, request: Request, user: CurrentUser, db: Db, page: str = "1"
) -> HTMLResponse:
    """Every move and refusal of a reorganize plan, paged (data-safety rule 5)."""
    plan = _plan(db, plan_id, user)
    rows = [op for op in progress.with_done(db, plan) if op["op"] in ("move", "refused")]
    pages = max(1, -(-len(rows) // MOVES_PAGE))
    n = min(max(_num(page, 1), 1), pages)
    ctx = {
        "user": user,
        "plan": plan,
        "total": len(rows),
        "page": n,
        "pages": pages,
        "rows": rows[(n - 1) * MOVES_PAGE : n * MOVES_PAGE],
    }
    return templates.TemplateResponse(request, "pages/plan_moves.html", ctx)


def _act(
    request: Request,
    user: AuthUser,
    plan_id: int,
    action: str,
    db: Session,
    count: str,
    enqueue: Callable[[Session, datetime, int], None],
) -> RedirectResponse:
    plan = _plan(db, plan_id, user)
    if action == "confirm" and stale_delete_draft(db, plan, request.app.state.services.clock.now()):
        plan.status = "discarded"
        db.commit()  # the refusal below would roll the discard back
        raise HTTPException(409, "this delete preview is over a week old; make a new one")
    if action == "confirm" and plan.status in ("running", "done"):  # a second click: it took
        return RedirectResponse(f"/library/plans/{plan_id}", status_code=303)
    if action not in _allowed(db, plan, _days(request)):
        raise HTTPException(409, f"plan is {plan.status}")
    _check_count(plan, count)
    if action == "confirm":  # the worker deletes the request last; a refusal now aborts the confirm
        for op in plan.ops:
            if op["op"] == "request" and (req := db.get(AlbumRequest, op["request_id"])):
                check = db.begin_nested()
                try:
                    delete_row(db, req, user.id)
                finally:
                    check.rollback()  # checks only: nothing is deleted yet
    enqueue(db, datetime.now(UTC), plan.id)
    return RedirectResponse(f"/library/plans/{plan_id}", status_code=303)


@router.post("/plans/{plan_id}/confirm")
def confirm(
    plan_id: int, request: Request, user: CurrentUser, db: Db, count: Annotated[str, Form()] = ""
) -> RedirectResponse:
    return _act(request, user, plan_id, "confirm", db, count, enqueue_merge)


@router.post("/plans/{plan_id}/discard")
def discard(plan_id: int, request: Request, user: CurrentUser, db: Db) -> RedirectResponse:
    plan = _plan(db, plan_id, user)
    if "discard" not in _allowed(db, plan, _days(request)):
        raise HTTPException(409, f"plan is {plan.status}")
    plan.status = "discarded"  # a preview that never ran: DB only
    return RedirectResponse(f"/library/plans/{plan_id}", status_code=303)


@router.post("/plans/{plan_id}/resume")
def resume(
    plan_id: int, request: Request, user: CurrentUser, db: Db, count: Annotated[str, Form()] = ""
) -> RedirectResponse:
    return _act(request, user, plan_id, "resume", db, count, enqueue_merge)


@router.post("/plans/{plan_id}/undo")
def undo(
    plan_id: int, request: Request, user: CurrentUser, db: Db, count: Annotated[str, Form()] = ""
) -> RedirectResponse:
    return _act(request, user, plan_id, "undo", db, count, enqueue_undo)


def _delete_plan(merger: Merger, track_ids: list[int]) -> RedirectResponse:
    try:
        plan_id = merger.build_delete(track_ids)
    except (PlanInProgress, DeleteRefused) as e:
        raise HTTPException(409, str(e)) from None
    return RedirectResponse(f"/library/plans/{plan_id}", status_code=303)


@router.post("/unverified/delete")
def delete_unverified_album(
    request: Request,
    user: User,
    db: Db,
    merger: Merge,
    album: Annotated[str, Form()] = "",
    albumartist: Annotated[str, Form()] = "",
    year: Annotated[str, Form()] = "",
) -> RedirectResponse:
    items = recheck.group(recheck.read_unverified(db)).get((albumartist, album, year))
    if not items:
        raise HTTPException(404, "no unverified tracks for that album")
    db.commit()  # the login's last_seen write must not hold the SQLite lock while build runs
    return _delete_plan(merger, [t.id for t in items])


_KINDS = ("verified", "unverified", "untagged", "pending")


def _album_tracks(
    db: Session, kind: str, group: str, albumartist: str, album: str, year: str
) -> list[Track]:
    rows = albums.tracks_of(db, kind, group, (albumartist, album, year)) if kind in _KINDS else []
    if not rows:
        raise HTTPException(404, "no such album")
    return rows


@router.get("/albums")
def browse(
    request: Request, user: CurrentUser, db: Db, q: str = "", page: str = "1"
) -> HTMLResponse:
    found = albums.search(db, q)
    pages = max(1, -(-len(found) // albums.PAGE))
    n = min(max(_num(page, 1), 1), pages)
    ctx = {
        "user": user,
        "q": q,
        "page": n,
        "pages": pages,
        "total": len(found),
        "albums": found[(n - 1) * albums.PAGE : n * albums.PAGE],
        "pending": db.scalar(select(func.count()).where(Track.album.is_(None))),
        "drift": layout.drift(db, request.app.state.settings.path_template),
    }
    return templates.TemplateResponse(request, "pages/albums.html", ctx)


@router.get("/album")
def album_page(
    request: Request,
    user: CurrentUser,
    db: Db,
    kind: str,
    group: str = "",
    albumartist: str = "",
    album: str = "",
    year: str = "",
) -> HTMLResponse:
    ctx = {
        "user": user,
        "kind": kind,
        "group": group,
        "albumartist": albumartist,
        "album": album,
        "year": year,
        "tracks": _album_tracks(db, kind, group, albumartist, album, year),
        # the unverified list's empty-album groups (#270): their delete is its own route
        "bare": sorted(
            k
            for k in recheck.group(recheck.read_unverified(db))
            if kind == "untagged" and not k[0] and not k[1]
        ),
    }
    return templates.TemplateResponse(request, "pages/album.html", ctx)


@router.post("/album/delete")
def delete_album(
    user: User,
    db: Db,
    merger: Merge,
    kind: Annotated[str, Form()],
    group: Annotated[str, Form()] = "",
    albumartist: Annotated[str, Form()] = "",
    album: Annotated[str, Form()] = "",
    year: Annotated[str, Form()] = "",
) -> RedirectResponse:
    if kind not in ("verified", "unverified"):  # untagged and pending: per-track actions only
        raise HTTPException(404, "no such album")
    rows = _album_tracks(db, kind, group, albumartist, album, year)
    db.commit()  # the login's last_seen write must not hold the SQLite lock while build runs
    return _delete_plan(merger, [t.id for t in rows])


@router.post("/tracks/{track_id}/delete")
def delete_track(track_id: int, user: User, db: Db, merger: Merge) -> RedirectResponse:
    if db.get(Track, track_id) is None:
        raise HTTPException(404, "no such track")
    db.commit()
    return _delete_plan(merger, [track_id])


class _NoRestore(HTTPException):
    """The file has no restorable tags; the page shows why, the restore POST answers 409."""


_tag_lock = threading.Lock()  # one restore at a time: a double click must not run twice


def _sha256(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _tag_target(
    db: Session, fo: FileOps, track_id: int, snapshot_id: int | None = None
) -> tuple[Track, TagSnapshot, tags.Snapshot]:
    """The track, the snapshot to restore and the file's current tags. The snapshot must come
    from the import that produced the file now at the path (its journaled hash matches the
    file), so a different recording that once had this path, or a file changed since, has none."""
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(404, "no such track")
    if not fo.has(track.path):
        raise HTTPException(409, "the file is missing from the library")
    for plan in db.scalars(select(Plan).where(Plan.status.in_(("draft", "running", "undoing")))):
        if plan.status == "draft" and not live_job(db, plan.id):
            continue  # an unconfirmed preview changes nothing
        for op in plan.ops:
            if track.id == op.get("track_id") or track.path in (
                op.get("old", {}).get("path"),
                op.get("new_path"),
            ):
                raise HTTPException(409, f"plan {plan.id} is changing this track")
    path = fo.library / track.path
    q = (
        select(TagSnapshot)
        .join(FileOp, FileOp.id == TagSnapshot.op_id)
        .where(
            TagSnapshot.file_path == track.path,
            FileOp.root == "library",
            FileOp.status == "done",
            FileOp.dst_hash == _sha256(path),
        )
        .order_by(TagSnapshot.id.desc())
    )
    snap = db.scalars(q if snapshot_id is None else q.where(TagSnapshot.id == snapshot_id)).first()
    if snap is None:
        raise _NoRestore(409, "There are no earlier tags to restore for this file.")
    try:
        current = tags.read_all(path)
    except Exception:
        raise _NoRestore(409, "The file's tags cannot be read.") from None
    if current["format"] != snap.tags["format"]:
        raise _NoRestore(409, "The snapshot is of a different file format.")
    return track, snap, current


@router.get("/tracks/{track_id}/tags")
def track_tags(track_id: int, request: Request, user: User, db: Db, fo: Files) -> HTMLResponse:
    try:
        track, snap, current = _tag_target(db, fo, track_id)
    except _NoRestore as e:
        track = db.get(Track, track_id)
        assert track is not None  # _tag_target found it before raising
        ctx = {"user": user, "track": track, "snapshot": None, "unavailable": e.detail}
        return templates.TemplateResponse(request, "pages/track_tags.html", ctx)
    ctx = {
        "user": user,
        "track": track,
        "snapshot": snap,
        "changes": tags.changes(current, snap.tags),
    }
    return templates.TemplateResponse(request, "pages/track_tags.html", ctx)


@router.post("/tracks/{track_id}/tags")
def restore_track_tags(
    track_id: int,
    request: Request,
    user: User,
    db: Db,
    fo: Files,
    snapshot_id: Annotated[int, Form()],
) -> RedirectResponse:
    with _tag_lock:
        track, snap, current = _tag_target(db, fo, track_id, snapshot_id)
        if not tags.changes(current, snap.tags):
            raise HTTPException(409, "the file already has these tags")
        rel, old, tid = track.path, snap.tags, track.id
        db.commit()  # free the SQLite write lock (login last_seen) for the snapshot write
        try:
            tag_undo.restore_tags(
                fo,
                request.app.state.session_factory,
                request.app.state.services.clock,
                tid,
                rel,
                old,
            )
        except FileExistsError:
            raise HTTPException(
                409, "the file changed while restoring; reload and try again"
            ) from None
    return RedirectResponse(f"/library/tracks/{tid}/tags", status_code=303)
