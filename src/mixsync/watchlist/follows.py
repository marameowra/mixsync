"""Follow CRUD and the poll chain's start; the poll itself is in poller.py."""

from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Select, delete, select, update
from sqlalchemy.orm import Session

from mixsync.core.clock import aware
from mixsync.core.jobs import JobKind, JobState
from mixsync.db.models.watchlist import Follow, FollowHit, WatchedEntity
from mixsync.db.models.work import Job
from mixsync.db.queue import USER_PRIORITY, enqueue_in

KINDS = ("artist", "label")
RELEASE_TYPES = ("album", "ep", "single", "compilation", "live")
DEFAULT_TYPES = ["album", "ep"]
LABEL_WEEKLY_CAP = 5
CHECK_NOW_GAP = timedelta(hours=1)  # a button must not turn into a poll loop on MusicBrainz


def waiting(entity_id: int) -> Select[Any]:
    """Checks still to come. A running one does not count: it may have read the follows already."""
    return select(Job).where(
        Job.kind == JobKind.WATCHLIST_POLL,
        Job.status.in_((JobState.QUEUED, JobState.LEASED, JobState.RETRY_WAIT)),
        Job.payload["entity_id"].as_integer() == entity_id,
    )


def ensure_poll(s: Session, now: datetime, e: WatchedEntity) -> None:
    """Make sure a check is queued; none is when the chain ended or the entity is new."""
    if s.scalar(waiting(e.id).limit(1)) is None:
        e.next_check_at = now
        enqueue_in(s, now, JobKind.WATCHLIST_POLL, {"entity_id": e.id})


def _run_now(s: Session, now: datetime, e: WatchedEntity) -> None:
    """Bring the queued check forward to now, or queue one."""
    e.next_check_at = now
    moved = cast(
        CursorResult[Any],
        s.execute(
            update(Job)
            .where(
                Job.kind == JobKind.WATCHLIST_POLL,
                Job.status.in_((JobState.QUEUED, JobState.RETRY_WAIT)),
                Job.payload["entity_id"].as_integer() == e.id,
            )
            .values(run_after=now, priority=USER_PRIORITY if s.info.get("user") else Job.priority)
        ),
    )
    if moved.rowcount == 0:
        ensure_poll(s, now, e)


def add_follow(s: Session, now: datetime, user_id: int, kind: str, mbid: str, name: str) -> Follow:
    """Follow an artist or label; an existing follow of the same entity is returned unchanged."""
    e = s.scalar(
        select(WatchedEntity).where(WatchedEntity.kind == kind, WatchedEntity.mbid == mbid)
    )
    if e is None:
        e = WatchedEntity(kind=kind, mbid=mbid)
        s.add(e)
        s.flush()
    f = s.scalar(select(Follow).where(Follow.user_id == user_id, Follow.entity_id == e.id))
    created = f is None
    if f is None:
        f = Follow(
            user_id=user_id,
            entity_id=e.id,
            name=name[:255],
            release_types=list(DEFAULT_TYPES),
            weekly_cap=LABEL_WEEKLY_CAP if kind == "label" else None,
            created_at=now,
        )
        s.add(f)
        s.flush()
    if created:
        _run_now(s, now, e)
    else:
        ensure_poll(s, now, e)
    return f


def remove_follow(s: Session, follow: Follow) -> None:
    """Drops the follow and its history; requests it made stay."""
    s.execute(delete(FollowHit).where(FollowHit.follow_id == follow.id))
    s.delete(follow)


def check_now(s: Session, now: datetime, e: WatchedEntity) -> bool:
    """Bring the entity's next check forward to now. False if it was checked within the gap."""
    if e.last_checked_at and now - aware(e.last_checked_at) < CHECK_NOW_GAP:
        return False
    _run_now(s, now, e)
    return True
