import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mixsync.acquire.lifecycle import BUSY, DONE, IN_PROGRESS
from mixsync.acquire.merge import DeleteRefused, PlanInProgress
from mixsync.acquire.request_delete import NotDeletable, live_states
from mixsync.acquire.request_delete import deletable as is_deletable
from mixsync.acquire.request_delete import delete_request as drop_request
from mixsync.acquire.requests import REQUEST_BUDGET, create_request
from mixsync.core.capabilities import Capability
from mixsync.core.jobs import LIVE, JobKind, JobState
from mixsync.db.auth import AuthUser
from mixsync.db.models.library import Plan, Track
from mixsync.db.models.work import JOB_REQUEST_ID, Job
from mixsync.db.models.work import Request as AlbumRequest
from mixsync.web.deps import CurrentUser, Db, Merge, Namer, require
from mixsync.web.names import resolve_listed
from mixsync.web.routes.review import parse_release_mbid
from mixsync.web.templating import render

log = logging.getLogger(__name__)
router = APIRouter(prefix="/requests")
User = Annotated[AuthUser, Depends(require(Capability.request))]


def _waiting(db: Session, ids: list[int], now: datetime) -> set[int]:
    """Requests whose next step is due but the worker has not taken it yet."""
    rid = JOB_REQUEST_ID
    due = select(rid).where(rid.in_(ids), Job.status == JobState.QUEUED, Job.run_after <= now)
    return set(db.scalars(due))


def _delete_plans(db: Session, ids: list[int]) -> dict[int, int]:
    """Request id -> its delete plan that is confirmed but not done (queued, running or failed).
    A draft counts once confirmed: a MERGE or UNDO_MERGE job for it is live (merge.live_job)."""
    confirmed = (
        select(Job.id)
        .where(
            Job.kind.in_([JobKind.MERGE, JobKind.UNDO_MERGE]),
            Job.status.in_(LIVE),
            Job.payload["plan_id"].as_integer() == Plan.id,
        )
        .exists()
    )
    q = select(Plan.id, Plan.ops).where(
        Plan.kind == "delete",
        or_(Plan.status.in_(["running", "failed"]), and_(Plan.status == "draft", confirmed)),
    )
    found: dict[int, int] = {}
    for plan_id, ops in db.execute(q.order_by(Plan.id)):
        for op in ops:
            if op["op"] == "request" and op["request_id"] in ids:
                found[op["request_id"]] = plan_id
    return found


@router.get("")
def request_list(
    request: Request,
    user: User,
    db: Db,
    namer: Namer,
    ids: Annotated[list[int] | None, Query()] = None,  # the live region polls for the rows shown
) -> HTMLResponse:
    query = select(AlbumRequest).where(
        AlbumRequest.user_id == user.id, AlbumRequest.migration_id.is_(None)
    )
    if ids is not None:
        query = query.where(AlbumRequest.id.in_(ids))
    items = db.scalars(query.order_by(AlbumRequest.id.desc())).all()
    live = live_states(db, [r.id for r in items])
    deletable = {r.id for r in items if is_deletable(r, live)}
    queued = _waiting(db, [r.id for r in items], datetime.now(UTC))
    candidates = [r.id for r in items if r.status not in BUSY]
    music = set(  # requests with library tracks from their imports: one query for the page
        db.scalars(
            select(AlbumRequest.id)
            .join(Track, Track.request_id == AlbumRequest.id)
            .where(AlbumRequest.id.in_(candidates))
            .distinct()
        )
    )
    return render(
        request,
        "pages/requests.html",
        "pages/requests_table.html",
        {
            "user": user,
            "items": items,
            "in_progress": any(r.status in IN_PROGRESS for r in items),
            "live_url": "/requests?" + "&".join(f"ids={r.id}" for r in items),
            "deletable": deletable,
            "delete_plans": _delete_plans(db, [r.id for r in items]),
            "music": music,
            "queued": queued,
            "budget": REQUEST_BUDGET,
            "names": resolve_listed(db, namer, {("release", r.release_mbid) for r in items}),
        },
    )


@router.post("")
def new_request(user: User, db: Db, release: Annotated[str, Form()]) -> RedirectResponse:
    mbid = parse_release_mbid(release)
    again = select(AlbumRequest.id).where(  # a double submit: the first one is still in progress
        AlbumRequest.user_id == user.id,
        AlbumRequest.release_mbid == mbid,
        AlbumRequest.status.not_in(DONE),
        AlbumRequest.provisional.is_(False),
        AlbumRequest.migration_id.is_(None),  # /requests hides these
    )
    if db.scalar(again.limit(1)) is None:
        create_request(db, datetime.now(UTC), user.id, mbid)
    return RedirectResponse("/requests", status_code=303)


def delete_row(db: Session, req: AlbumRequest, user_id: int) -> None:
    try:
        drop_request(db, req, user_id)
    except NotDeletable as e:
        raise HTTPException(409, str(e)) from e


@router.post("/delete-imported")
def delete_imported(request: Request, user: User, db: Db) -> RedirectResponse:
    mine = select(AlbumRequest).where(
        AlbumRequest.user_id == user.id,
        AlbumRequest.status == "imported",
        AlbumRequest.migration_id.is_(None),
    )
    ids = list(db.scalars(mine.with_only_columns(AlbumRequest.id)))
    db.commit()  # the session is spent: each request below gets its own short transaction (#422)
    for rid in ids:
        try:  # a refused row rolls back only its own changes
            with request.app.state.session_factory.begin() as s:
                if req := s.scalar(mine.where(AlbumRequest.id == rid)):  # still imported
                    delete_row(s, req, user.id)
        except HTTPException:
            continue
        except IntegrityError as e:
            log.warning("clear imported: kept request %s: %s", rid, e)
    return RedirectResponse("/requests", status_code=303)


@router.post("/{request_id}/delete")
def delete_request(request_id: int, user: User, db: Db) -> RedirectResponse:
    req = db.get(AlbumRequest, request_id)
    if req is None or req.user_id != user.id:
        raise HTTPException(403 if req else 404, "not your request")
    delete_row(db, req, user.id)
    return RedirectResponse("/requests", status_code=303)


@router.post("/{request_id}/delete-with-music")
def delete_with_music(
    request_id: int, user: CurrentUser, db: Db, merger: Merge
) -> RedirectResponse:
    """The dry run of "delete this request and its music"; the request goes on confirm."""
    req = db.get(AlbumRequest, request_id)
    if req is None:
        raise HTTPException(404, "no such request")
    if req.user_id != user.id and Capability.library_edit not in user.capabilities:
        raise HTTPException(403, "not your request")
    ids = list(db.scalars(select(Track.id).where(Track.request_id == req.id)))
    if not ids:
        raise HTTPException(404, "this request imported no music")
    db.commit()  # the login's last_seen write must not hold the SQLite lock while build runs
    try:
        plan_id = merger.build_delete(ids, req)
    except (PlanInProgress, DeleteRefused) as e:
        raise HTTPException(409, str(e)) from None
    return RedirectResponse(f"/library/plans/{plan_id}", status_code=303)
