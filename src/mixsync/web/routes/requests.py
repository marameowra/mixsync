import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import ColumnElement, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mixsync.acquire.requests import create_request
from mixsync.core.capabilities import Capability
from mixsync.core.jobs import LIVE, JobState
from mixsync.db import decisions
from mixsync.db.auth import AuthUser
from mixsync.db.models.discovery import ProvisionalHold
from mixsync.db.models.matching import MatchDecision
from mixsync.db.models.watchlist import FollowHit
from mixsync.db.models.work import Job
from mixsync.db.models.work import Request as AlbumRequest
from mixsync.discovery.playlists import DONE, is_dead_end
from mixsync.web.deps import Db, require
from mixsync.web.routes.review import parse_release_mbid
from mixsync.web.templating import templates

log = logging.getLogger(__name__)
router = APIRouter(prefix="/requests")
User = Annotated[AuthUser, Depends(require(Capability.request))]


_BUSY = ("downloading", "verifying", "importing")


def _of(req_id: int) -> ColumnElement[bool]:
    return Job.payload["request_id"].as_integer() == req_id


def _waiting(db: Session, ids: list[int], now: datetime) -> set[int]:
    """Requests whose next step is due but the worker has not taken it yet."""
    rid = Job.payload["request_id"].as_integer()
    due = select(rid).where(rid.in_(ids), Job.status == JobState.QUEUED, Job.run_after <= now)
    return set(db.scalars(due))


def _live_states(db: Session, ids: list[int]) -> dict[int, set[JobState]]:
    rid = Job.payload["request_id"].as_integer()
    states: dict[int, set[JobState]] = {}
    for req_id, status in db.execute(
        select(rid, Job.status).where(rid.in_(ids), Job.status.in_(LIVE))
    ):
        states.setdefault(req_id, set()).add(status)
    return states


def _deletable(req: AlbumRequest, live: dict[int, set[JobState]]) -> bool:
    """A no_results or partial request always has a queued repeat search, which delete
    removes with it."""
    if req.status in _BUSY or is_dead_end(req):
        return False
    blocking = (
        {JobState.LEASED, JobState.RUNNING}
        if req.status in ("no_results", "partial")
        else set(LIVE)
    )
    return not (live.get(req.id, set()) & blocking)


@router.get("")
def request_list(request: Request, user: User, db: Db) -> HTMLResponse:
    items = db.scalars(
        select(AlbumRequest)
        .where(AlbumRequest.user_id == user.id, AlbumRequest.migration_id.is_(None))
        .order_by(AlbumRequest.id.desc())
    ).all()
    live = _live_states(db, [r.id for r in items])
    deletable = {r.id for r in items if _deletable(r, live)}
    queued = _waiting(db, [r.id for r in items], datetime.now(UTC))
    return templates.TemplateResponse(
        request,
        "pages/requests.html",
        {"user": user, "items": items, "deletable": deletable, "queued": queued},
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


def _delete(db: Session, req: AlbumRequest, user_id: int) -> None:
    """Drops the row and its finished jobs; files on disk and decisions (the audit record) stay.
    Raises HTTPException(409) when the request cannot be deleted; the caller rolls back."""
    if req.migration_id is not None:
        raise HTTPException(409, "a migration album cannot be deleted")
    if is_dead_end(req):
        raise HTTPException(409, "discovery keeps this as a dead end")
    if not _deletable(req, _live_states(db, [req.id])):
        raise HTTPException(409, "request is in progress")
    # A decision still pending for this request would sit in /review with nothing to act on.
    # Stage 1 decisions are never in /review and decisions.act rejects them.
    pending = select(MatchDecision).where(
        MatchDecision.status == decisions.PENDING, MatchDecision.stage == 2
    )
    for d in db.scalars(pending):
        if d.evidence.get("request_id") == req.id:
            try:
                decisions.act(db, d.id, user_id, "reject")
            except decisions.DecisionStateError:
                raise HTTPException(409, "a review is acting on this request") from None
    # A worker may have claimed the repeat search since _deletable checked; never delete its job.
    held = (JobState.LEASED, JobState.RUNNING)
    db.execute(delete(Job).where(_of(req.id), Job.status.not_in(held)))
    if db.scalar(select(Job.id).where(_of(req.id)).limit(1)) is not None:
        raise HTTPException(409, "request is in progress")  # get_db rolls the whole delete back
    # Keep the hit (its status still stops the watchlist re-requesting) and the hold; drop the link.
    for model in (FollowHit, ProvisionalHold):
        db.execute(update(model).where(model.request_id == req.id).values(request_id=None))
    db.delete(req)


@router.post("/delete-imported")
def delete_imported(user: User, db: Db) -> RedirectResponse:
    mine = select(AlbumRequest).where(
        AlbumRequest.user_id == user.id,
        AlbumRequest.status == "imported",
        AlbumRequest.migration_id.is_(None),
    )
    for req in db.scalars(mine).all():
        try:
            # On SQLite the savepoint nests only because the session already began a transaction
            # (the last_seen_at autoflush); db/engine.py has no pysqlite SAVEPOINT recipe.
            with db.begin_nested():  # a refused row rolls back only its own changes
                _delete(db, req, user.id)
        except HTTPException:
            continue
        except IntegrityError as e:
            log.warning("clear imported: kept request %s: %s", req.id, e)
    return RedirectResponse("/requests", status_code=303)


@router.post("/{request_id}/delete")
def delete_request(request_id: int, user: User, db: Db) -> RedirectResponse:
    req = db.get(AlbumRequest, request_id)
    if req is None or req.user_id != user.id:
        raise HTTPException(403 if req else 404, "not your request")
    _delete(db, req, user.id)
    return RedirectResponse("/requests", status_code=303)
