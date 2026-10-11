"""The watchlist poll: one MusicBrainz search per followed entity, fanned out to its followers."""

import random
from collections.abc import Callable
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.pipeline import Handler
from mixsync.acquire.requests import create_request
from mixsync.core.clock import Clock
from mixsync.core.jobs import JobKind
from mixsync.core.protocols import ReleaseWatch
from mixsync.core.types import ReleaseGroupHit
from mixsync.db.models.library import Track
from mixsync.db.models.watchlist import Follow, FollowHit, WatchedEntity
from mixsync.db.models.work import Request
from mixsync.db.queue import JobRecord, enqueue_in

OVERLAP = timedelta(days=7)  # covers search-index lag
CAP_WINDOW = timedelta(days=7)
Rng = Callable[[float, float], float]


def interval_for(last_release: str | None, today: date) -> timedelta:
    """3 days if the entity released in the last 12 months, 7 if 1-2 years ago, else 14."""
    if not last_release:
        return timedelta(days=14)
    y, m, d = (int(p) for p in (last_release.split("-") + ["1", "1"])[:3])
    age = today - date(y, m, d)
    if age <= timedelta(days=365):
        return timedelta(days=3)
    return timedelta(days=7 if age <= timedelta(days=730) else 14)


def jitter(interval: timedelta, rng: Rng = random.uniform) -> timedelta:
    """A random point in 0.5x..1.5x the interval, so a check never lands at a fixed clock time."""
    return interval * rng(0.5, 1.5)


def _date_ok(d: str, cutoff: str) -> bool:
    return d >= cutoff[: len(d)]  # "2026" and "2026-03" are as precise as MB knows


def _filtered(f: Follow, h: ReleaseGroupHit) -> bool:
    if h.type not in f.release_types:
        return True
    if f.only_after_follow and not _date_ok(h.date, f.created_at.date().isoformat()):
        return True
    return bool(f.min_date) and not _date_ok(h.date, f.min_date or "")


def _owned(s: Session, h: ReleaseGroupHit) -> bool:
    return (
        s.scalar(select(Track.id).where(Track.release_group_mbid == h.group_id).limit(1))
        is not None
    )


def _requested(s: Session, user_id: int, h: ReleaseGroupHit) -> bool:
    manual = select(Request.id).where(
        Request.user_id == user_id, Request.release_mbid.in_(h.release_ids)
    )
    other = (
        select(FollowHit.id)
        .join(Follow, Follow.id == FollowHit.follow_id)
        .where(
            Follow.user_id == user_id,
            FollowHit.release_group_mbid == h.group_id,
            FollowHit.status == "requested",
        )
    )
    return s.scalar(manual.limit(1)) is not None or s.scalar(other.limit(1)) is not None


def _over_cap(s: Session, f: Follow, now: datetime) -> bool:
    if f.weekly_cap is None:
        return False
    n = s.scalar(
        select(func.count(FollowHit.id)).where(
            FollowHit.follow_id == f.id,
            FollowHit.status == "requested",
            FollowHit.created_at >= now - CAP_WINDOW,
        )
    )
    return (n or 0) >= f.weekly_cap


def request_hit(s: Session, now: datetime, f: Follow, hit: FollowHit) -> None:
    req = create_request(s, now, f.user_id, hit.release_mbid, source="watchlist")
    hit.status, hit.request_id = "requested", req.id


def apply_hit(s: Session, now: datetime, f: Follow, h: ReleaseGroupHit) -> None:
    """Record what the follow does with one release group; creates the request if it passes."""
    row = FollowHit(
        follow_id=f.id,
        release_group_mbid=h.group_id,
        # shortcut: any release of the group; the pipeline settles the edition.
        release_mbid=h.release_ids[0],
        artist=h.artist[:255],
        title=h.title[:255],
        release_date=h.date,
        status="skipped_filter",
        created_at=now,
    )
    s.add(row)
    if _filtered(f, h):
        return
    if _owned(s, h):
        row.status = "skipped_owned"
    elif _requested(s, f.user_id, h):
        row.status = "skipped_requested"
    elif _over_cap(s, f, now):
        row.status = "skipped_cap"
    else:
        request_hit(s, now, f, row)
    s.flush()  # the cap count and the next hit's dedupe see this one


class Watcher:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        clock: Clock,
        watch: ReleaseWatch,
        rng: Rng = random.uniform,
    ) -> None:
        self.sessions, self.clock, self.watch, self.rng = sessions, clock, watch, rng

    def handlers(self) -> dict[JobKind, Handler]:
        return {JobKind.WATCHLIST_POLL: self.poll}

    async def poll(self, job: JobRecord) -> None:
        eid = job.payload["entity_id"]
        with self.sessions() as s:
            e = s.get(WatchedEntity, eid)
            if e is None or not s.scalar(select(Follow.id).where(Follow.entity_id == eid).limit(1)):
                return  # nobody follows it any more: the chain ends, a new follow restarts it
            kind, mbid, last = e.kind, e.mbid, e.last_checked_at
        # The first check has no lower bound, to learn how active the entity is.
        since = (last - OVERLAP).date().isoformat() if last else None
        hits = await self.watch.new_release_groups(kind, mbid, since)
        now = self.clock.now()
        with self.sessions.begin() as s:
            e = s.get(WatchedEntity, eid)
            if e is None:
                return
            for f in s.scalars(select(Follow).where(Follow.entity_id == eid).order_by(Follow.id)):
                seen = set(
                    s.scalars(
                        select(FollowHit.release_group_mbid).where(FollowHit.follow_id == f.id)
                    )
                )
                # Nothing older than the follow (less the overlap) is new to this follower.
                floor = (f.created_at - OVERLAP).date().isoformat()
                for h in sorted(hits, key=lambda h: h.date):
                    if h.group_id not in seen and h.release_ids and _date_ok(h.date, floor):
                        apply_hit(s, now, f, h)
            dates = [d for d in (e.last_release_date, *(h.date for h in hits)) if d]
            e.last_release_date, e.last_checked_at = max(dates, default=None), now
            nxt = now + jitter(interval_for(e.last_release_date, now.date()), self.rng)
            e.next_check_at = nxt
            enqueue_in(
                s,
                now,
                JobKind.WATCHLIST_POLL,
                {"entity_id": eid},
                run_after=nxt,
                idempotency_key=f"watchlist:{eid}:{nxt.isoformat()}",
            )
