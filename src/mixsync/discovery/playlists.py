"""The discovery playlist generator: build a pick list from ListenBrainz, ask for what is not
owned, and push what is to the user's Navidrome. Retention of the provisional tracks is slice 7."""

import logging
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Select, select, update
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.pipeline import Handler
from mixsync.acquire.requests import create_request
from mixsync.core.clock import Clock
from mixsync.core.jobs import JobKind, JobState
from mixsync.core.protocols import HistorySource, UserLibrary
from mixsync.core.types import RecordingRef
from mixsync.db.models.accounts import UserLink
from mixsync.db.models.discovery import DiscoveryPlaylist, ProvisionalHold, ProvisionalTrack
from mixsync.db.models.library import Track
from mixsync.db.models.work import Job, Request
from mixsync.db.queue import USER_PRIORITY, JobRecord, enqueue_in
from mixsync.db.secretbox import SecretBox, link_secret
from mixsync.watchlist.poller import Rng, jitter

log = logging.getLogger(__name__)

DEFAULT_SOURCES: list[dict[str, Any]] = [{"type": "weekly_exploration"}]
BUILD_NOW_GAP = timedelta(hours=1)  # a button must not turn into a ListenBrainz poll loop
NO_LINKS = "link ListenBrainz/Navidrome on /account"
DONE = ("imported", "owned", "failed", "skipped")  # requests that will not bring a file
LibraryFor = Callable[[str, str], UserLibrary]  # (Navidrome username, password)


def waiting(playlist_id: int) -> Select[Any]:
    """Builds still to come. A running one does not count: it may have read the playlist."""
    return select(Job).where(
        Job.kind == JobKind.DISCOVERY_BUILD,
        Job.status.in_((JobState.QUEUED, JobState.LEASED, JobState.RETRY_WAIT)),
        Job.payload["playlist_id"].as_integer() == playlist_id,
    )


def ensure_build(s: Session, now: datetime, pl: DiscoveryPlaylist) -> None:
    """Make sure a build is queued, due at the playlist's next_build_at (or now)."""
    if s.scalar(waiting(pl.id).limit(1)) is None:
        due = pl.next_build_at.replace(tzinfo=UTC) if pl.next_build_at else now
        pl.next_build_at = due
        enqueue_in(s, now, JobKind.DISCOVERY_BUILD, {"playlist_id": pl.id}, run_after=due)


def ensure_default(s: Session, now: datetime, user_id: int) -> None:
    """The first default playlist of a user who has linked ListenBrainz."""
    has_lb = s.scalar(
        select(UserLink.id).where(UserLink.user_id == user_id, UserLink.service == "listenbrainz")
    )
    mine = select(DiscoveryPlaylist.id).where(DiscoveryPlaylist.user_id == user_id)
    if has_lb is None or s.scalar(mine.limit(1)) is not None:
        return
    pl = DiscoveryPlaylist(user_id=user_id, name="Discovery", sources=list(DEFAULT_SOURCES))
    s.add(pl)
    s.flush()
    ensure_build(s, now, pl)


def build_now(s: Session, now: datetime, pl: DiscoveryPlaylist) -> bool:
    """Bring the queued build forward to now. False if built within the gap."""
    if pl.last_built_at and now - pl.last_built_at.replace(tzinfo=UTC) < BUILD_NOW_GAP:
        return False
    pl.next_build_at = now
    moved = cast(
        CursorResult[Any],
        s.execute(
            update(Job)
            .where(
                Job.kind == JobKind.DISCOVERY_BUILD,
                Job.status.in_((JobState.QUEUED, JobState.RETRY_WAIT)),
                Job.payload["playlist_id"].as_integer() == pl.id,
            )
            .values(run_after=now, priority=USER_PRIORITY if s.info.get("user") else Job.priority)
        ),
    )
    if moved.rowcount == 0:
        ensure_build(s, now, pl)
    return True


class Discovery:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        clock: Clock,
        history: HistorySource,
        library_for: LibraryFor,
        box: SecretBox,
        rng: Rng = random.uniform,
        discover_library: str = "discover",  # the Navidrome library over /data/discover
    ) -> None:
        self.sessions, self.clock, self.history = sessions, clock, history
        self.library_for, self.box, self.rng = library_for, box, rng
        self.discover_library = discover_library

    def handlers(self) -> dict[JobKind, Handler]:
        return {JobKind.DISCOVERY_BUILD: self.build, JobKind.DISCOVERY_PUSH: self.push}

    def start_chains(self) -> None:
        """On startup: every playlist gets a build queued if it has none waiting."""
        now = self.clock.now()
        with self.sessions.begin() as s:
            for uid in s.scalars(
                select(UserLink.user_id).where(UserLink.service == "listenbrainz")
            ).all():
                ensure_default(s, now, uid)
            for pl in s.scalars(select(DiscoveryPlaylist)):
                ensure_build(s, now, pl)

    def _schedule_next(self, s: Session, now: datetime, pl: DiscoveryPlaylist, job_id: int) -> None:
        """Queue the next build, keyed on this job so a re-run adds none."""
        if s.scalar(waiting(pl.id).where(Job.id != job_id).limit(1)) is not None:
            return
        nxt = now + jitter(timedelta(days=pl.cadence_days), self.rng)
        pl.next_build_at = nxt
        enqueue_in(
            s,
            now,
            JobKind.DISCOVERY_BUILD,
            {"playlist_id": pl.id},
            run_after=nxt,
            idempotency_key=f"discovery:{pl.id}:after:{job_id}",
        )

    def _links(self, s: Session, user_id: int) -> dict[str, tuple[str, str]] | None:
        """service -> (external username, secret) for both links, or None if either is missing."""
        out: dict[str, tuple[str, str]] = {}
        for link in s.scalars(select(UserLink).where(UserLink.user_id == user_id)):
            if (secret := link_secret(self.box, link)) is not None:
                out[link.service] = (link.external_username, secret)
        return out if {"listenbrainz", "navidrome"} <= out.keys() else None

    async def build(self, job: JobRecord) -> None:
        pid = job.payload["playlist_id"]
        try:
            await self._build(job, pid)
        except Exception:
            # Retries are the queue's; once they run out the chain must go on.
            with self.sessions.begin() as s:
                if pl := s.get(DiscoveryPlaylist, pid):
                    self._schedule_next(s, self.clock.now(), pl, job.id)
            raise

    async def _candidates(
        self, sources: list[dict[str, Any]], size: int, name: str, token: str
    ) -> list[RecordingRef]:
        found: list[RecordingRef] = []
        for src in sources:
            match src["type"]:
                case "weekly_exploration":
                    found += await self.history.weekly_exploration(name, token)
                case "recommendations":
                    found += await self.history.recommendations(name, token, size)
                case "lb_radio":
                    mode = src.get("mode", "easy")
                    found += await self.history.lb_radio(src["prompt"], mode, token)
                case _:
                    log.warning("discovery: unknown source %r ignored", src["type"])
        asked = any(src["type"] == "recommendations" for src in sources)
        if len({r.recording_mbid for r in found}) < size and not asked:
            found += await self.history.recommendations(name, token, size)
        unique: dict[str, RecordingRef] = {}
        for r in found:
            unique.setdefault(r.recording_mbid, r)  # source order, first one wins
        return list(unique.values())

    async def _build(self, job: JobRecord, pid: int) -> None:
        with self.sessions() as s:
            pl = s.get(DiscoveryPlaylist, pid)
            if pl is None:
                return
            links = self._links(s, pl.user_id)
            sources, size, ratio = list(pl.sources) or DEFAULT_SOURCES, pl.size, pl.unowned_ratio
        if links is None:
            with self.sessions.begin() as s:
                if pl := s.get(DiscoveryPlaylist, pid):
                    pl.last_error = NO_LINKS
                    self._schedule_next(s, self.clock.now(), pl, job.id)
            return
        lb_name, lb_token = links["listenbrainz"]
        cands = await self._candidates(sources, size, lb_name, lb_token)
        mbids = [c.recording_mbid for c in cands]
        with self.sessions() as s:
            in_library = set(
                s.scalars(select(Track.recording_mbid).where(Track.recording_mbid.in_(mbids)))
            )
            provisional = {
                p.recording_mbid: p
                for p in s.scalars(
                    select(ProvisionalTrack).where(
                        ProvisionalTrack.recording_mbid.in_(mbids),
                        ProvisionalTrack.status == "active",
                    )
                )
            }
        owned = in_library | provisional.keys()
        unowned = [c for c in cands if c.recording_mbid not in owned][: round(size * ratio)]
        fill = [c for c in cands if c.recording_mbid in owned][: size - len(unowned)]
        picked = {c.recording_mbid for c in (*unowned, *fill)}
        missing = [c.recording_mbid for c in unowned if not c.release_mbid]
        found = await self.history.recording_metadata(missing) if missing else {}
        now = self.clock.now()
        with self.sessions.begin() as s:
            pl = s.get(DiscoveryPlaylist, pid)
            if pl is None:
                return
            live = self._live_requests(s)
            for c in unowned:
                mbid = c.recording_mbid
                release = c.release_mbid or (found[mbid].release_mbid if mbid in found else None)
                if not release:
                    log.info("discovery: no release known for %s; not requested", mbid)
                    continue
                hold = self._hold(s, pl, mbid)
                if mbid not in live:
                    live[mbid] = create_request(
                        s,
                        now,
                        pl.user_id,
                        release,
                        "discovery",
                        provisional=True,
                        recordings=[mbid],
                    ).id
                hold.request_id = live[mbid]
            for c in fill:  # a track another user's request imported still needs this user's hold
                pt = provisional.get(c.recording_mbid)
                if pt and c.recording_mbid not in in_library:
                    hold = self._hold(s, pl, c.recording_mbid)
                    hold.provisional_track_id = pt.id
                    if hold.expires_at is None:
                        imported = pt.imported_at.replace(tzinfo=UTC)
                        hold.expires_at = imported + timedelta(days=pl.retention_days)
            pl.entries = [
                {"recording_mbid": c.recording_mbid, "artist": c.artist, "title": c.title}
                for c in cands
                if c.recording_mbid in picked
            ]
            pl.last_built_at = now
            pl.last_error = (
                None if pl.entries else "ListenBrainz returned no tracks; nothing pushed"
            )
            if pl.entries:
                enqueue_in(
                    s,
                    now,
                    JobKind.DISCOVERY_PUSH,
                    {"playlist_id": pl.id},
                    idempotency_key=f"discovery-push:{pl.id}:build:{job.id}",
                )
            self._schedule_next(s, now, pl, job.id)

    @staticmethod
    def _live_requests(s: Session) -> dict[str, int]:
        """recording MBID -> a provisional request still on its way to a file."""
        live: dict[str, int] = {}
        for r in s.scalars(select(Request).where(Request.provisional, Request.status.not_in(DONE))):
            for m in r.recording_mbids:
                live.setdefault(m, r.id)
        return live

    @staticmethod
    def _hold(s: Session, pl: DiscoveryPlaylist, mbid: str) -> ProvisionalHold:
        hold = s.scalar(
            select(ProvisionalHold).where(
                ProvisionalHold.user_id == pl.user_id, ProvisionalHold.recording_mbid == mbid
            )
        )
        if hold is None:
            hold = ProvisionalHold(user_id=pl.user_id, recording_mbid=mbid, playlist_id=pl.id)
            s.add(hold)
            s.flush()
        return hold

    async def push(self, job: JobRecord) -> None:
        pid = job.payload["playlist_id"]
        with self.sessions() as s:
            pl = s.get(DiscoveryPlaylist, pid)
            if pl is None or not pl.entries:
                return
            links = self._links(s, pl.user_id)
            if links is None:
                with self.sessions.begin() as w:
                    if (p := w.get(DiscoveryPlaylist, pid)) is not None:
                        p.last_error = NO_LINKS
                return
            entries, name, existing = list(pl.entries), pl.name, pl.navidrome_playlist_id
            mbids: list[str] = [e["recording_mbid"] for e in entries]
            main = {
                m
                for m in s.scalars(
                    select(Track.recording_mbid).where(Track.recording_mbid.in_(mbids))
                )
                if m
            }
            discover = set(
                s.scalars(
                    select(ProvisionalTrack.recording_mbid).where(
                        ProvisionalTrack.recording_mbid.in_(mbids),
                        ProvisionalTrack.status == "active",
                    )
                )
            )
        lib = self.library_for(*links["navidrome"])
        folder = await lib.music_folder_id(self.discover_library) if discover - main else None
        ids: list[str] = []
        for e in entries:
            m = e["recording_mbid"]
            if m not in main and m not in discover:
                continue  # not imported yet: it joins on the push after its import
            song = await lib.resolve(m, e["title"], e["artist"], None if m in main else folder)
            if song is None:
                log.info("discovery: %s is not in Navidrome yet; a later push adds it", m)
            elif song not in ids:
                ids.append(song)
        if not ids:
            return
        new_id = await lib.push_playlist(f"MixSync: {name}", ids, existing)
        with self.sessions.begin() as s:
            if (pl := s.get(DiscoveryPlaylist, pid)) is not None:
                pl.navidrome_playlist_id = new_id
