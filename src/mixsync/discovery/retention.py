"""Retention of provisional discovery tracks: keep (star, plays, manual), promote kept ones into
the main library, expire the rest, trash a file only once no user's hold wants it."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from mixsync.acquire.pipeline import Handler
from mixsync.acquire.requests import LATE_PUSH, RESCAN_DELAY, enqueue_rescan
from mixsync.core.clock import aware
from mixsync.core.errors import PermanentError
from mixsync.core.jobs import LIVE, JobKind
from mixsync.core.matching import AlbumInfo
from mixsync.core.protocols import MetadataProvider, UserLibrary
from mixsync.db import canonical as canonical_db
from mixsync.db.models.discovery import DiscoveryPlaylist, ProvisionalHold, ProvisionalTrack
from mixsync.db.models.library import Track
from mixsync.db.models.work import Job
from mixsync.db.queue import JobRecord, enqueue_in
from mixsync.discovery.playlists import JITTER_CAP, Discovery
from mixsync.library import canonical
from mixsync.library.fileops import FileOps
from mixsync.library.importer import Importer

log = logging.getLogger(__name__)

EVERY = timedelta(days=1)
UNKNOWN_NOTE = " discovery tracks not found in Navidrome; not expiring them"
SOON = timedelta(days=7)


def expiring_soon(
    s: Session, now: datetime, user_id: int, within: timedelta = SOON
) -> list[ProvisionalHold]:
    """The user's imported, undecided holds that expire within `within`, soonest first."""
    return list(
        s.scalars(
            select(ProvisionalHold)
            .where(
                ProvisionalHold.user_id == user_id,
                ProvisionalHold.provisional_track_id.is_not(None),
                ProvisionalHold.kept_at.is_(None),
                ProvisionalHold.expired_at.is_(None),
                ProvisionalHold.expires_at <= now + within,
            )
            .order_by(ProvisionalHold.expires_at)
        )
    )


def keep(s: Session, now: datetime, user_id: int, recording_mbid: str) -> bool:
    """Manual keep. False if the user has no live hold on the recording."""
    hold = s.scalar(
        select(ProvisionalHold).where(
            ProvisionalHold.user_id == user_id,
            ProvisionalHold.recording_mbid == recording_mbid,
            ProvisionalHold.expired_at.is_(None),
        )
    )
    if hold is None:
        return False
    hold.kept_at, hold.keep_reason = hold.kept_at or now, hold.keep_reason or "manual"
    return True


@dataclass
class Row:
    hold_id: int
    track_id: int
    mbid: str
    playlist_id: int
    keep_plays: int
    expires_at: datetime | None
    kept: bool
    song_id: str | None
    title: str
    artist: str


def waiting(user_id: int) -> Select[Any]:
    return select(Job).where(
        Job.kind == JobKind.DISCOVERY_RETAIN,
        Job.status.in_(LIVE),
        Job.payload["user_id"].as_integer() == user_id,
    )


class Retention:
    def __init__(
        self,
        discovery: Discovery,
        importer: Importer,
        fileops: FileOps,
        metadata: MetadataProvider,
        media: tuple[str, ...] = ("Digital Media", "CD"),
        countries: tuple[str, ...] = ("XW",),
    ) -> None:
        self.d, self.importer, self.fileops, self.metadata = discovery, importer, fileops, metadata
        self.media, self.countries = media, countries

    def handlers(self) -> dict[JobKind, Handler]:
        return {JobKind.DISCOVERY_RETAIN: self.retain}

    def start_chains(self) -> None:
        """On startup: every user with a playlist gets a retention run queued if none waits."""
        now = self.d.clock.now()
        with self.d.sessions.begin() as s:
            for uid in s.scalars(select(DiscoveryPlaylist.user_id).distinct()).all():
                if s.scalar(waiting(uid).limit(1)) is None:
                    enqueue_in(s, now, JobKind.DISCOVERY_RETAIN, {"user_id": uid})

    def _schedule_next(self, s: Session, now: datetime, uid: int, job_id: int) -> None:
        if s.scalar(waiting(uid).where(Job.id != job_id).limit(1)) is not None:
            return
        nxt = now + EVERY + min(EVERY * 0.1, JITTER_CAP) * self.d.rng(-1, 1)
        enqueue_in(
            s,
            now,
            JobKind.DISCOVERY_RETAIN,
            {"user_id": uid},
            run_after=nxt,
            idempotency_key=f"discovery-retain:{uid}:after:{job_id}",
        )

    async def retain(self, job: JobRecord) -> None:
        uid = job.payload["user_id"]
        try:
            await self._retain(job, uid)
        except Exception:
            with self.d.sessions.begin() as s:  # the queue retries; the chain must go on
                self._schedule_next(s, self.d.clock.now(), uid, job.id)
            raise
        with self.d.sessions.begin() as s:
            self._schedule_next(s, self.d.clock.now(), uid, job.id)

    def _rows(self, s: Session, uid: int) -> list[Row]:
        out: list[Row] = []
        q = (
            select(ProvisionalHold, ProvisionalTrack, DiscoveryPlaylist)
            .join(ProvisionalTrack, ProvisionalTrack.id == ProvisionalHold.provisional_track_id)
            .join(DiscoveryPlaylist, DiscoveryPlaylist.id == ProvisionalHold.playlist_id)
            .where(
                ProvisionalHold.user_id == uid,
                ProvisionalHold.expired_at.is_(None),
                ProvisionalTrack.status == "active",
            )
        )
        for h, pt, pl in s.execute(q):
            by_mbid: dict[str, dict[str, Any]] = {e["recording_mbid"]: e for e in pl.entries}
            e = by_mbid.get(h.recording_mbid, {})
            out.append(
                Row(
                    h.id,
                    pt.id,
                    h.recording_mbid,
                    pl.id,
                    pl.keep_plays,
                    h.expires_at and aware(h.expires_at),
                    h.kept_at is not None,
                    pt.navidrome_song_id,
                    pt.title or e.get("title", ""),  # old rows: what the playlist says
                    pt.artist or e.get("artist", ""),
                )
            )
        return out

    def _note(self, rows: list[Row], why: str) -> None:
        with self.d.sessions.begin() as s:
            for pid in {r.playlist_id for r in rows}:
                if pl := s.get(DiscoveryPlaylist, pid):
                    pl.last_error = why

    async def _retain(self, job: JobRecord, uid: int) -> None:
        with self.d.sessions() as s:
            rows = self._rows(s, uid)
            links, why = self.d.links(s, uid, frozenset({"navidrome"}))
        if not rows:
            return
        if links is None:
            return self._note(
                rows, why
            )  # nothing is expired or trashed without a look at Navidrome
        lib = self.d.library_for(*links["navidrome"])
        reasons: dict[int, str] = {}  # hold id -> why it is kept
        try:
            folder = await lib.music_folder_id(self.d.discover_library)
            starred = await lib.starred_song_ids()  # once per user per run, like the counts
            for r in rows:
                if r.song_id is None and not r.kept:
                    r.song_id = await lib.resolve(r.mbid, r.title, r.artist, folder)
            counts = await self._counts(lib, rows)
            for r in rows:  # a cached id Navidrome no longer knows: look the song up again
                if r.song_id and not r.kept and r.song_id not in counts:
                    r.song_id = await lib.resolve(r.mbid, r.title, r.artist, folder)
                    counts |= await self._counts(lib, [r])
        except PermanentError as e:
            return self._note(rows, f"Navidrome: {e}")
        for r in rows:
            if r.kept:
                reasons[r.hold_id] = "manual"
            elif r.song_id in starred:
                reasons[r.hold_id] = "star"
            elif r.song_id and counts.get(r.song_id, 0) >= r.keep_plays:
                reasons[r.hold_id] = "plays"
        now = self.d.clock.now()
        promote: set[int] = set()
        changed: set[str] = set()  # recordings whose holds just changed
        unknown: dict[int, int] = {}  # playlist id -> holds left alone for want of a song id
        with self.d.sessions.begin() as s:
            for r in rows:
                hold = s.get_one(ProvisionalHold, r.hold_id)  # fresh: a Keep may have come since
                if hold.expired_at is not None:
                    continue
                why = reasons.get(r.hold_id) or (
                    "manual" if hold.kept_at or hold.keep_reason else None
                )
                if why:
                    hold.kept_at = hold.kept_at or now
                    hold.keep_reason = hold.keep_reason or why
                    promote.add(r.track_id)
                    changed.add(r.mbid)
                elif r.song_id is None:  # could be starred; never expire what cannot be checked
                    unknown[r.playlist_id] = unknown.get(r.playlist_id, 0) + 1
                elif hold.expires_at and now >= aware(hold.expires_at):
                    hold.expired_at = now
                    changed.add(r.mbid)
                pt = s.get_one(ProvisionalTrack, r.track_id)
                pt.navidrome_song_id = r.song_id
            for pid in {r.playlist_id for r in rows}:  # our note only; other errors stay
                pl = s.get_one(DiscoveryPlaylist, pid)
                ours = (pl.last_error or "").endswith(UNKNOWN_NOTE)
                if pid in unknown and (pl.last_error is None or ours):
                    pl.last_error = f"{unknown[pid]}{UNKNOWN_NOTE}"
                elif pid not in unknown and ours:
                    pl.last_error = None
        for tid in promote:
            try:
                await self._promote(tid)
            except Exception:  # kept stays kept: tomorrow's run tries again
                log.warning("discovery: promoting track %s failed", tid, exc_info=True)
        trashed = self._trash_unwanted(changed)
        if changed:
            self._push_after(changed, job.id)
        log.info("discovery retention: user %s kept %s, trashed %s", uid, len(promote), trashed)

    @staticmethod
    async def _counts(lib: UserLibrary, rows: list[Row]) -> dict[str, int]:
        ids = sorted({r.song_id for r in rows if r.song_id and not r.kept})
        return await lib.play_counts(ids) if ids else {}

    def _trash_unwanted(self, mbids: set[str]) -> int:
        """Trash the discover copy of each still-active track whose every hold is expired."""
        n = 0
        for mbid in mbids:
            with self.d.sessions.begin() as s:
                pt = s.scalar(
                    select(ProvisionalTrack).where(
                        ProvisionalTrack.recording_mbid == mbid, ProvisionalTrack.status == "active"
                    )
                )
                live = s.scalar(
                    select(ProvisionalHold.id).where(
                        ProvisionalHold.recording_mbid == mbid,
                        or_(
                            ProvisionalHold.expired_at.is_(None),
                            ProvisionalHold.kept_at.is_not(None),
                        ),
                    )
                )
                if pt is None or live is not None:
                    continue  # a hold still waiting or kept wants the file
                if self.fileops.has(pt.path, "discover"):
                    pt.trash_path = self.fileops.trash(pt.path, f"expire-{pt.id}", "discover")
                pt.status = "trashed"
                n += 1
        return n

    async def _promote(self, track_id: int) -> None:
        with self.d.sessions() as s:
            pt = s.get_one(ProvisionalTrack, track_id)
            if pt.status != "active":
                return
            path, mbid = pt.path, pt.recording_mbid
            release, acoustid = pt.release_mbid, pt.acoustid_id
            rel = s.scalar(select(Track.path).where(Track.recording_mbid == mbid))
        batch = f"promote-{track_id}"  # one batch id: a resumed promote skips what is in
        if rel is None:  # not already in the main library
            if release is None:
                raise PermanentError(f"track {track_id} has no release to import under")
            rel = await self._import(
                self.fileops.roots["discover"] / path, mbid, release, acoustid, batch
            )
        # The copy goes before the status flips: a failure here leaves the row active, and the
        # next run finds the tracks row, skips the import and trashes again (has() is idempotent).
        trash = (
            self.fileops.trash(path, batch, "discover")
            if self.fileops.has(path, "discover")
            else None
        )
        with self.d.sessions.begin() as s:
            pt = s.get_one(ProvisionalTrack, track_id)
            pt.status, pt.promoted_path, pt.trash_path = "promoted", rel, trash
            holds = s.scalars(
                select(ProvisionalHold).where(
                    ProvisionalHold.recording_mbid == mbid, ProvisionalHold.expired_at.is_(None)
                )
            )
            for h in holds:  # every user's playlist now resolves it in the main library
                h.kept_at = h.kept_at or self.d.clock.now()
                h.keep_reason = h.keep_reason or "promoted"

    async def _import(
        self, src: Path, mbid: str, release: str, acoustid: str | None, batch: str
    ) -> str:
        """Through the normal importer, filed under the group's canonical release when the
        track is on it (no bonus-track canonical switch: a rare edge, left to the merge plans)."""
        album = await self.metadata.get_release(release)
        track = next((t for t in album.tracks if t.recording_id == mbid), None)
        if track is None:
            raise PermanentError(f"{mbid} is not on release {release}")
        file = (src, track, acoustid)
        if cid := await self._canonical_id(album):
            if cid != album.release_id:
                canon = await self.metadata.get_release(cid)
                mapped, _ = canonical.split([file], canon.tracks)
                if mapped:
                    album, file = canon, mapped[0]
        with self.d.sessions() as s:  # the worker runs jobs one at a time; this covers awaits
            if have := s.scalar(select(Track.path).where(Track.recording_mbid == mbid)):
                return have
        (result,) = await asyncio.to_thread(
            self.importer.import_release, [file], album, "verified", batch
        )
        if result.rel is None:
            raise RuntimeError(result.error)
        return result.rel

    async def _canonical_id(self, album: AlbumInfo) -> str | None:
        group = album.release_group_id
        if not group:
            return None
        with self.d.sessions() as s:
            if stored := canonical_db.get(s, group):
                return stored
        releases = await self.metadata.browse_group_releases(group)
        if not releases:
            return None
        pick = canonical.choose(releases, self.media, self.countries)
        with self.d.sessions.begin() as s:
            return canonical_db.store(s, group, pick)

    def _push_after(self, mbids: set[str], job_id: int) -> None:
        """Navidrome rescans, then each affected playlist is pushed (twice, like an import)."""
        now = self.d.clock.now()
        with self.d.sessions.begin() as s:
            enqueue_rescan(s, now)
            pids = set(
                s.scalars(
                    select(ProvisionalHold.playlist_id).where(
                        ProvisionalHold.recording_mbid.in_(mbids)
                    )
                )
            )
            for pid in pids:
                for suffix, delay in (("", 2 * RESCAN_DELAY), (":late", LATE_PUSH)):
                    enqueue_in(
                        s,
                        now,
                        JobKind.DISCOVERY_PUSH,
                        {"playlist_id": pid},
                        run_after=now + delay,
                        idempotency_key=f"discovery-push:{pid}:retain:{job_id}{suffix}",
                    )
