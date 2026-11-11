"""Deleting a request row: the checks and the deletion, shared by the web routes and the worker
(a delete plan removes its request as its last step)."""

from sqlalchemy import ColumnElement, delete, select, update
from sqlalchemy.orm import Session

from mixsync.acquire import review
from mixsync.acquire.lifecycle import is_busy, is_dead_end, is_waiting
from mixsync.core.jobs import LIVE, JobState
from mixsync.db.models.discovery import ProvisionalHold
from mixsync.db.models.watchlist import FollowHit
from mixsync.db.models.work import JOB_REQUEST_ID, Job
from mixsync.db.models.work import Request as AlbumRequest


class NotDeletable(ValueError):
    """The request cannot be deleted now; the message says why."""


def _of(req_id: int) -> ColumnElement[bool]:
    return JOB_REQUEST_ID == req_id


def live_states(db: Session, ids: list[int]) -> dict[int, set[JobState]]:
    rid = JOB_REQUEST_ID
    states: dict[int, set[JobState]] = {}
    for req_id, status in db.execute(
        select(rid, Job.status).where(rid.in_(ids), Job.status.in_(LIVE))
    ):
        states.setdefault(req_id, set()).add(status)
    return states


def deletable(req: AlbumRequest, live: dict[int, set[JobState]]) -> bool:
    """A no_results or partial request always has a queued repeat search, which delete
    removes with it."""
    if is_busy(req) or is_dead_end(req):
        return False
    blocking = {JobState.LEASED, JobState.RUNNING} if is_waiting(req) else set(LIVE)
    return not (live.get(req.id, set()) & blocking)


def delete_request(db: Session, req: AlbumRequest, user_id: int) -> None:
    """Drops the row and its finished jobs; files on disk and decisions (the audit record) stay.
    Raises NotDeletable when the request cannot be deleted; the caller rolls back."""
    if req.migration_id is not None:
        raise NotDeletable("a migration album cannot be deleted")
    if is_dead_end(req):
        raise NotDeletable("discovery keeps this as a dead end")
    if not deletable(req, live_states(db, [req.id])):
        raise NotDeletable("request is in progress")
    # A decision still pending for this request would sit in /review with nothing to act on.
    try:
        review.cancel(db, req.id, user_id)
    except review.Refused as e:
        raise NotDeletable(str(e)) from e
    # A worker may have claimed the repeat search since `deletable` checked; never delete its job.
    held = (JobState.LEASED, JobState.RUNNING)
    db.execute(delete(Job).where(_of(req.id), Job.status.not_in(held)))
    if db.scalar(select(Job.id).where(_of(req.id)).limit(1)) is not None:
        raise NotDeletable("request is in progress")  # the caller rolls the whole delete back
    # Keep the hit (its status still stops the watchlist re-requesting) and the hold; drop the link.
    for model in (FollowHit, ProvisionalHold):
        db.execute(update(model).where(model.request_id == req.id).values(request_id=None))
    db.delete(req)
