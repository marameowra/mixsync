"""The watchlist poll: one MusicBrainz search per followed entity, fanned out to its followers."""

import random
from collections.abc import Callable
from datetime import date, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.pipeline import Handler
from mixsync.acquire.requests import LIVE, create_request
from mixsync.core.clock import Clock
from mixsync.core.jobs import JobKind
from mixsync.core.protocols import ReleaseWatch
from mixsync.core.types import ReleaseGroupHit
from mixsync.db import canonical as canonical_db
from mixsync.db.models.library import Track
from mixsync.db.models.watchlist import Follow, FollowHit, WatchedEntity
from mixsync.db.models.work import Job, Request
from mixsync.db.queue import JobRecord, enqueue_in
from mixsync.library import canonical
from mixsync.watchlist.follows import ensure_poll, waiting

OVERLAP = timedelta(days=7)  # covers search-index lag
CAP_WINDOW = timedelta(days=7)
FIRST_WINDOW = timedelta(days=365)
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
    return bool(f.min_date) and not _date_ok(h.date, f.min_date or "")


def is_owned(s: Session, group_id: str) -> bool:
    return (
        s.scalar(select(Track.id).where(Track.release_group_mbid == group_id).limit(1)) is not None
    )


def is_requested(s: Session, user_id: int, group_id: str, release_ids: tuple[str, ...]) -> bool:
    manual = select(Request.id).where(
        Request.user_id == user_id,
        or_(Request.release_mbid.in_(release_ids), Request.release_group_mbid == group_id),
    )
    other = (
        select(FollowHit.id)
        .join(Follow, Follow.id == FollowHit.follow_id)
        .where(
            Follow.user_id == user_id,
            FollowHit.release_group_mbid == group_id,
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
    req = create_request(
        s,
        now,
        f.user_id,
        hit.release_mbid,
        source="watchlist",
        release_group_mbid=hit.release_group_mbid,
    )
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
    if is_owned(s, h.group_id):
        row.status = "skipped_owned"
    elif is_requested(s, f.user_id, h.group_id, h.release_ids):
        row.status = "skipped_requested"
    elif _over_cap(s, f, now):
        row.status = "skipped_cap"
    else:
        request_hit(s, now, f, row)
    s.flush()  # the cap count and the next hit's dedupe see this one


def queue_picks(s: Session, now: datetime, f: Follow, picks: list[ReleaseGroupHit]) -> None:
    """Back-catalogue picks: a "picked" history row each and one job to resolve them, so the
    web request does no MusicBrainz work."""
    for h in picks:
        row = s.scalar(
            select(FollowHit).where(
                FollowHit.follow_id == f.id, FollowHit.release_group_mbid == h.group_id
            )
        )
        if row is None:
            row = FollowHit(
                follow_id=f.id,
                release_group_mbid=h.group_id,
                release_mbid="",
                release_date=h.date,
                artist=f.name[:255],
                title=h.title[:255],
                created_at=now,
            )
            s.add(row)
        else:  # keep the old row's details, and what it was, in case the pick finds nothing
            row.prev_status = row.status
        row.status = "picked"
    if picks:
        s.flush()
        enqueue_in(s, now, JobKind.WATCHLIST_PICK, {"follow_id": f.id})


class Watcher:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        clock: Clock,
        watch: ReleaseWatch,
        rng: Rng = random.uniform,
        media: tuple[str, ...] = ("Digital Media", "CD"),
        countries: tuple[str, ...] = ("XW",),
    ) -> None:
        self.sessions, self.clock, self.watch, self.rng = sessions, clock, watch, rng
        self.media, self.countries = media, countries

    def handlers(self) -> dict[JobKind, Handler]:
        return {JobKind.WATCHLIST_POLL: self.poll, JobKind.WATCHLIST_PICK: self.pick}

    def start_chains(self) -> None:
        """On startup: every followed entity gets a check queued if it has none waiting."""
        now = self.clock.now()
        with self.sessions.begin() as s:
            for e in s.scalars(
                select(WatchedEntity).where(
                    select(Follow.id).where(Follow.entity_id == WatchedEntity.id).exists()
                )
            ):
                ensure_poll(s, now, e)
            # A pick job that failed for good leaves its rows "picked"; give each follow a new one.
            stuck = select(FollowHit.follow_id).where(FollowHit.status == "picked").distinct()
            for fid in s.scalars(stuck).all():
                live = s.scalar(
                    select(Job.id)
                    .where(
                        Job.kind == JobKind.WATCHLIST_PICK,
                        Job.status.in_(LIVE),
                        Job.payload["follow_id"].as_integer() == fid,
                    )
                    .limit(1)
                )
                if live is None:
                    enqueue_in(s, now, JobKind.WATCHLIST_PICK, {"follow_id": fid})

    def _schedule_next(self, s: Session, now: datetime, e: WatchedEntity, job_id: int) -> None:
        """Queue the next check, keyed on this job so a re-run adds none, unless another check
        is already waiting (a new follow may have queued one)."""
        if s.scalar(waiting(e.id).where(Job.id != job_id).limit(1)) is not None:
            return
        nxt = now + jitter(interval_for(e.last_release_date, now.date()), self.rng)
        e.next_check_at = nxt
        enqueue_in(
            s,
            now,
            JobKind.WATCHLIST_POLL,
            {"entity_id": e.id},
            run_after=nxt,
            idempotency_key=f"watchlist:{e.id}:after:{job_id}",
        )

    async def pick(self, job: JobRecord) -> None:
        """Turn the follow's picked release groups into requests. Each is its own transaction,
        and none is held open across a MusicBrainz call, so a retry carries on where it stopped."""
        with self.sessions() as s:
            ids = list(
                s.scalars(
                    select(FollowHit.id).where(
                        FollowHit.follow_id == job.payload["follow_id"],
                        FollowHit.status == "picked",
                    )
                )
            )
        for hid in ids:
            with self.sessions() as s:
                hit = s.get(FollowHit, hid)
                if hit is None or hit.status != "picked":
                    continue
                group = hit.release_group_mbid
                release = canonical_db.get(s, group)  # the phase 3 policy: stored, else chosen
            known: tuple[str, ...] = ()
            if release is None:
                releases = await self.watch.browse_group_releases(group)
                if not releases:  # nothing to request: let it be picked again
                    with self.sessions.begin() as s:
                        if (hit := s.get(FollowHit, hid)) and hit.status == "picked":
                            if hit.prev_status:  # an earlier row: give it back as it was
                                hit.status, hit.prev_status = hit.prev_status, None
                            else:
                                s.delete(hit)
                    continue
                release = canonical.choose(releases, self.media, self.countries)
                known = tuple(r.release_id for r in releases)
            with self.sessions.begin() as s:
                hit = s.get(FollowHit, hid)
                f = hit and s.get(Follow, hit.follow_id)
                if hit is None or f is None or hit.status != "picked":
                    continue
                if is_owned(s, group):
                    hit.status = "skipped_owned"
                elif is_requested(s, f.user_id, group, (release, *known)):
                    hit.status = "skipped_requested"
                else:
                    hit.release_mbid = release
                    request_hit(s, self.clock.now(), f, hit)

    async def poll(self, job: JobRecord) -> None:
        eid = job.payload["entity_id"]
        try:
            await self._poll(job, eid)
        except Exception:
            # Retries are the queue's (with backoff); once they run out the chain must go on.
            with self.sessions.begin() as s:
                e = s.get(WatchedEntity, eid)
                if e is not None and s.scalar(select(Follow.id).where(Follow.entity_id == eid)):
                    self._schedule_next(s, self.clock.now(), e, job.id)
            raise

    async def _poll(self, job: JobRecord, eid: int) -> None:
        now = self.clock.now()
        with self.sessions() as s:
            e = s.get(WatchedEntity, eid)
            if e is None or not s.scalar(select(Follow.id).where(Follow.entity_id == eid).limit(1)):
                return  # nobody follows it any more: a new follow queues a check
            kind, mbid, last = e.kind, e.mbid, e.last_checked_at
        # The first check looks back a year, which is enough to tell how active the entity is.
        since = ((last - OVERLAP) if last else (now - FIRST_WINDOW)).date().isoformat()
        hits = await self.watch.new_release_groups(kind, mbid, since, fresh=True)
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
                floor = (
                    (f.created_at - OVERLAP).date().isoformat()
                )  # older is not new to a follower
                for h in sorted(hits, key=lambda h: h.date):
                    if h.group_id not in seen and h.release_ids and _date_ok(h.date, floor):
                        apply_hit(s, now, f, h)
            dates = [d for d in (e.last_release_date, *(h.date for h in hits)) if d]
            e.last_release_date, e.last_checked_at = max(dates, default=None), now
            self._schedule_next(s, now, e, job.id)
