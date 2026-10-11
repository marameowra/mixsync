"""Follow CRUD and the poll chain's start; the poll itself is in poller.py."""

from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Select, delete, select, update
from sqlalchemy.orm import Session

from mixsync.acquire.requests import LIVE
from mixsync.core.jobs import JobKind, JobState
from mixsync.db.models.watchlist import Follow, FollowHit, WatchedEntity
from mixsync.db.models.work import Job
from mixsync.db.queue import enqueue_in

KINDS = ("artist", "label")
RELEASE_TYPES = ("album", "ep", "single", "compilation", "live")
DEFAULT_TYPES = ["album", "ep"]
LABEL_WEEKLY_CAP = 5
CHECK_NOW_GAP = timedelta(hours=1)  # a button must not turn into a poll loop on MusicBrainz


def _waiting(entity_id: int) -> Select[Any]:
    return select(Job).where(
        Job.kind == JobKind.WATCHLIST_POLL,
        Job.status.in_(LIVE),
        Job.payload["entity_id"].as_integer() == entity_id,
    )


def _ensure_poll(s: Session, now: datetime, e: WatchedEntity) -> None:
    """Make sure a check is queued; none is when the chain ended or the entity is new."""
    if s.scalar(_waiting(e.id).limit(1)) is None:
        e.next_check_at = now
        enqueue_in(s, now, JobKind.WATCHLIST_POLL, {"entity_id": e.id})


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
    if f is None:
        f = Follow(
            user_id=user_id,
            entity_id=e.id,
            name=name[:255],
            release_types=list(DEFAULT_TYPES),
            only_after_follow=True,
            weekly_cap=LABEL_WEEKLY_CAP if kind == "label" else None,
            created_at=now,
        )
        s.add(f)
        s.flush()
    _ensure_poll(s, now, e)
    return f


def remove_follow(s: Session, follow: Follow) -> None:
    """Drops the follow and its history; requests it made stay."""
    s.execute(delete(FollowHit).where(FollowHit.follow_id == follow.id))
    s.delete(follow)


def check_now(s: Session, now: datetime, e: WatchedEntity) -> bool:
    """Bring the entity's next check forward to now. False if it was checked within the gap."""
    if e.last_checked_at and now - e.last_checked_at.replace(tzinfo=now.tzinfo) < CHECK_NOW_GAP:
        return False
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
            .values(run_after=now)
        ),
    )
    if moved.rowcount == 0:
        _ensure_poll(s, now, e)
    return True
