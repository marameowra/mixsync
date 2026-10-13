"""The discovery playlist generator: build a pick list from ListenBrainz, ask for what is not
owned, and push what is to the user's Navidrome. Retention of the provisional tracks is slice 7."""

import logging
import random
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Select, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.pipeline import Handler
from mixsync.acquire.requests import create_request
from mixsync.core.clock import Clock, aware
from mixsync.core.errors import AuthError, PermanentError
from mixsync.core.jobs import JobKind, JobState
from mixsync.core.protocols import HistorySource, UserLibrary
from mixsync.core.types import RecordingRef
from mixsync.db.models.accounts import UserLink
from mixsync.db.models.discovery import DiscoveryPlaylist, ProvisionalHold, ProvisionalTrack
from mixsync.db.models.library import Track
from mixsync.db.models.work import Job, Request
from mixsync.db.queue import USER_PRIORITY, JobRecord, enqueue_in
from mixsync.db.secretbox import SecretBox, link_secret
from mixsync.watchlist.poller import Rng

log = logging.getLogger(__name__)

DEFAULT_SOURCES: list[dict[str, Any]] = [{"type": "weekly_exploration"}]
BUILD_NOW_GAP = timedelta(hours=1)  # a button must not turn into a ListenBrainz poll loop
NO_LINKS = "link ListenBrainz/Navidrome on /account"
JITTER_CAP = timedelta(hours=6)  # "weekly" stays weekly: up to 10% of the cadence, this at most
LINK_NAMES = {"listenbrainz": "ListenBrainz", "navidrome": "Navidrome"}
# last_error prefixes a push writes; a good push clears only these, never a build's error
PUSH_ERRORS = ("Navidrome: ", "Navidrome needs relinking", "the '")
PARTIAL_RELEASE = "not on this release"  # an imported request's last_error (pipeline)
NOT_ON_RELEASE = "none of the wanted recordings"  # a failed request's last_error (pipeline)


def is_dead_end(r: Request) -> bool:
    """A provisional request whose release cannot give the wanted recording: asking again finds
    the same, so the row itself is the record that stops a rebuild re-requesting it."""
    err = r.last_error or ""
    return (
        r.provisional
        and r.status in ("failed", "imported")
        and (
            err.startswith(NOT_ON_RELEASE)
            or (r.status == "imported" and err.endswith(PARTIAL_RELEASE))
        )
    )


DONE = ("imported", "owned", "failed", "skipped")  # requests that will not bring a file
LibraryFor = Callable[[str, str], UserLibrary]  # (Navidrome username, password)


def waiting(playlist_id: int) -> Select[Any]:
    """Builds queued, leased, running or waiting to retry."""
    return select(Job).where(
        Job.kind == JobKind.DISCOVERY_BUILD,
        Job.status.in_((JobState.QUEUED, JobState.LEASED, JobState.RUNNING, JobState.RETRY_WAIT)),
        Job.payload["playlist_id"].as_integer() == playlist_id,
    )


def ensure_build(s: Session, now: datetime, pl: DiscoveryPlaylist) -> None:
    """Make sure a build is queued, due at the playlist's next_build_at (or now)."""
    if s.scalar(waiting(pl.id).limit(1)) is None:
        due = aware(pl.next_build_at) if pl.next_build_at else now
        pl.next_build_at = due
        enqueue_in(s, now, JobKind.DISCOVERY_BUILD, {"playlist_id": pl.id}, run_after=due)


def ensure_default(s: Session, now: datetime, user_id: int) -> None:
    """The first default playlist of a user who has linked ListenBrainz."""
    has_lb = s.scalar(
        select(UserLink.id).where(UserLink.user_id == user_id, UserLink.service == "listenbrainz")
    )
    if has_lb is None or _has_playlist(s, user_id):
        return
    pl = DiscoveryPlaylist(user_id=user_id, name="Discovery", sources=list(DEFAULT_SOURCES))
    try:
        with s.begin_nested():  # a racing caller may have made it: the unique key says so
            s.add(pl)
    except IntegrityError:
        return
    ensure_build(s, now, pl)


def _has_playlist(s: Session, user_id: int) -> bool:
    mine = select(DiscoveryPlaylist.id).where(DiscoveryPlaylist.user_id == user_id)
    return s.scalar(mine.limit(1)) is not None


def build_now(s: Session, now: datetime, pl: DiscoveryPlaylist) -> bool:
    """Bring the queued build forward to now. False if built within the gap."""
    if pl.last_built_at and now - aware(pl.last_built_at) < BUILD_NOW_GAP:
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
        main_library: str = "Music Library",  # Navidrome library names; None found: unrestricted
        discover_library: str = "discover",  # the library over /data/discover
    ) -> None:
        self.sessions, self.clock, self.history = sessions, clock, history
        self.library_for, self.box, self.rng = library_for, box, rng
        self.main_library, self.discover_library = main_library, discover_library

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
        cadence = timedelta(days=pl.cadence_days)
        nxt = now + cadence + min(cadence * 0.1, JITTER_CAP) * self.rng(-1, 1)
        pl.next_build_at = nxt
        enqueue_in(
            s,
            now,
            JobKind.DISCOVERY_BUILD,
            {"playlist_id": pl.id},
            run_after=nxt,
            idempotency_key=f"discovery:{pl.id}:after:{job_id}",
        )

    def links(
        self,
        s: Session,
        user_id: int,
        need: frozenset[str] = frozenset({"listenbrainz", "navidrome"}),
    ) -> tuple[dict[str, tuple[str, str]] | None, str]:
        """(service -> (external username, secret), "") when `need` links work, else (None, why)."""
        out: dict[str, tuple[str, str]] = {}
        relink: list[str] = []
        for link in s.scalars(select(UserLink).where(UserLink.user_id == user_id)):
            if (secret := link_secret(self.box, link)) is None:
                if link.service in need:
                    relink.append(LINK_NAMES.get(link.service, link.service))
            else:
                out[link.service] = (link.external_username, secret)
        if need <= out.keys():
            return out, ""
        return None, f"{relink[0]} needs relinking on /account" if relink else NO_LINKS

    def _note(self, pid: int, why: str | None) -> None:
        with self.sessions.begin() as s:
            if pl := s.get(DiscoveryPlaylist, pid):
                pl.last_error = why

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
            links, why = self.links(s, pl.user_id)
            sources, size, ratio = list(pl.sources) or DEFAULT_SOURCES, pl.size, pl.unowned_ratio
        if links is None:
            return self._stop(pid, job.id, why)
        lb_name, lb_token = links["listenbrainz"]
        try:
            cands = await self._candidates(sources, size, lb_name, lb_token)
        except AuthError:
            return self._stop(pid, job.id, "ListenBrainz rejected the token; relink it on /account")
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
            ever = set(  # imported at some point (a trashed copy is not "not on the release")
                s.scalars(
                    select(ProvisionalTrack.recording_mbid).where(
                        ProvisionalTrack.recording_mbid.in_(mbids)
                    )
                )
            )
        owned = in_library | provisional.keys()
        todo = [c for c in cands if c.recording_mbid not in owned]
        missing = [c.recording_mbid for c in todo if not c.release_mbid]
        found = await self.history.recording_metadata(missing) if missing else {}
        # (pick, release): those with no known release can't be asked for, so they take no slot
        ready: list[tuple[RecordingRef, str]] = []
        for c in todo:
            release = c.release_mbid or (
                found[c.recording_mbid].release_mbid if c.recording_mbid in found else None
            )
            if release:
                ready.append((c, release))
            else:
                log.info("discovery: %s has no usable release; not requested", c.recording_mbid)
        releases = {release for _, release in ready}
        with self.sessions() as s:
            dead = {  # a release the recording is not on: asking again finds the same
                (r.release_mbid, m)
                for r in s.scalars(
                    select(Request).where(
                        Request.provisional,
                        Request.status.in_(("failed", "imported")),
                        Request.release_mbid.in_(releases),
                    )
                )
                if is_dead_end(r)
                for m in r.recording_mbids
                if r.status == "failed" or m not in ever
            }
        asks: list[tuple[RecordingRef, str]] = []
        for c, release in ready:
            if (release, c.recording_mbid) in dead:
                log.info("discovery: %s has no usable release; not requested", c.recording_mbid)
            else:
                asks.append((c, release))
        asks = asks[: round(size * ratio)]
        fill = [c for c in cands if c.recording_mbid in owned][: size - len(asks)]
        picked = {c.recording_mbid for c in (*(c for c, _ in asks), *fill)}
        now = self.clock.now()
        with self.sessions.begin() as s:
            pl = s.get(DiscoveryPlaylist, pid)
            if pl is None:
                return
            live = self._live_requests(s)
            by_release: dict[str, list[str]] = {}  # one request per release, all its tracks
            for c, release in asks:
                if c.recording_mbid not in live:
                    by_release.setdefault(release, []).append(c.recording_mbid)
            for release, wanted in by_release.items():
                req = create_request(
                    s, now, pl.user_id, release, "discovery", provisional=True, recordings=wanted
                )
                live.update({m: req.id for m in wanted})
            for c, _ in asks:
                self._hold(s, now, pl, c.recording_mbid, download=True).request_id = live[
                    c.recording_mbid
                ]
            for c in fill:  # a track another user's request imported still needs this user's hold
                pt = provisional.get(c.recording_mbid)
                if pt and c.recording_mbid not in in_library:
                    hold = self._hold(s, now, pl, c.recording_mbid)
                    hold.provisional_track_id = pt.id
                    if hold.expires_at is None:
                        hold.expires_at = now + timedelta(days=pl.retention_days)
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

    def _stop(self, pid: int, job_id: int, why: str) -> None:
        """Record why this build did nothing, and keep the chain going."""
        with self.sessions.begin() as s:
            if pl := s.get(DiscoveryPlaylist, pid):
                pl.last_error = why
                self._schedule_next(s, self.clock.now(), pl, job_id)

    @staticmethod
    def _live_requests(s: Session) -> dict[str, int]:
        """recording MBID -> a provisional request still on its way to a file."""
        live: dict[str, int] = {}
        for r in s.scalars(select(Request).where(Request.provisional, Request.status.not_in(DONE))):
            for m in r.recording_mbids:
                live.setdefault(m, r.id)
        return live

    @staticmethod
    def _hold(
        s: Session, now: datetime, pl: DiscoveryPlaylist, mbid: str, download: bool = False
    ) -> ProvisionalHold:
        hold = s.scalar(
            select(ProvisionalHold).where(
                ProvisionalHold.user_id == pl.user_id, ProvisionalHold.recording_mbid == mbid
            )
        )
        if hold is None:
            hold = ProvisionalHold(user_id=pl.user_id, recording_mbid=mbid, playlist_id=pl.id)
            s.add(hold)
            s.flush()
        elif hold.expired_at is not None:  # picked again after it expired: a fresh hold
            hold.expired_at = hold.kept_at = hold.keep_reason = None
            # a download starts its clock at import (hold_imports); a track on disk, now
            hold.expires_at = None if download else now + timedelta(days=pl.retention_days)
            hold.provisional_track_id = None
            hold.playlist_id = pl.id
        return hold

    async def push(self, job: JobRecord) -> None:
        pid = job.payload["playlist_id"]
        with self.sessions() as s:
            pl = s.get(DiscoveryPlaylist, pid)
            if pl is None or not pl.entries:
                return
            links, why = self.links(s, pl.user_id)
            if links is None:
                return self._note(pid, why)
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
        note = None
        ids: list[str] = []
        try:
            # a main-library entry must not resolve to its discover copy: restrict both
            main_folder = await lib.music_folder_id(self.main_library) if main else None
            if main and main_folder is None:
                note = (
                    f"the '{self.main_library}' library is not visible to your "
                    "Navidrome account; searching all libraries"
                )
            folder = None
            if discover - main:
                folder = await lib.music_folder_id(self.discover_library)
                if folder is None:
                    note = (
                        f"the '{self.discover_library}' library is not visible to your "
                        "Navidrome account; searching all libraries"
                    )
            for e in entries:
                m = e["recording_mbid"]
                if m not in main and m not in discover:
                    continue  # not imported yet: it joins on the push after its import
                where = main_folder if m in main else folder
                song = await lib.resolve(m, e["title"], e["artist"], where)
                if song is None:
                    log.info("discovery: %s is not in Navidrome yet; a later push adds it", m)
                elif song not in ids:
                    ids.append(song)
            new_id = await lib.push_playlist(f"MixSync: {name}", ids, existing) if ids else None
        except PermanentError as e:  # AuthError, not authorized: retrying would not help
            return self._note(pid, f"Navidrome: {e}")
        with self.sessions.begin() as s:
            if (pl := s.get(DiscoveryPlaylist, pid)) is not None:
                if note or (pl.last_error or "").startswith(PUSH_ERRORS):
                    pl.last_error = note  # a good push clears what an earlier one reported
                pl.navidrome_playlist_id = new_id or pl.navidrome_playlist_id
