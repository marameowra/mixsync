"""Mirrored playlists: a user's read-only copy of a playlist curated elsewhere (CONTEXT.md).

A sync fetches the upstream list, resolves each track to a recording, stores the list in upstream
order and pushes the owned tracks to Navidrome, replacing the playlist there. Missing tracks are
requested only when the owner confirms (`request_missing`). Nothing here moves or deletes a file:
a track that leaves upstream leaves the playlist only.

Upstream adapters (Spotify file, ListenBrainz loves) implement `Upstream`, are registered by
`upstream_kind` in the worker, and make a user's mirror with `create`."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, cast

from sqlalchemy import CursorResult, Select, or_, select, update
from sqlalchemy.orm import Session

from mixsync.acquire.lifecycle import DONE
from mixsync.acquire.requests import create_request
from mixsync.core.clock import aware
from mixsync.core.errors import PermanentError
from mixsync.core.jobs import LIVE, JobKind, JobState
from mixsync.core.types import RecordingRef
from mixsync.db.models.discovery import MirroredPlaylist
from mixsync.db.models.library import Track
from mixsync.db.models.work import Job, Request
from mixsync.db.queue import USER_PRIORITY, JobRecord, enqueue_in
from mixsync.discovery.playlists import BUILD_NOW_GAP, JITTER_CAP, Discovery

log = logging.getLogger(__name__)

SOURCE = "playlist"  # Request.source of a confirmed missing track
UNRESOLVED, OWNED, MISSING = "unresolved", "owned", "missing"
REQUESTED, NO_RELEASE = "requested", "no known release"
EMPTY_UPSTREAM = "upstream returned no tracks; kept the last list"
NONE_FOUND = "Navidrome found none of the owned tracks; skipped push"
LOVES = "listenbrainz_loves"
SPOTIFY_API = "spotify_api"
# kinds whose tracks never become a Request (Spotify's terms, docs/design/services-etiquette.md)
NEVER_REQUEST = frozenset({SPOTIFY_API})
# kinds whose newly listed missing tracks are requested without asking, once the owner confirmed
AUTO_REQUEST = frozenset({LOVES})


@dataclass(frozen=True)
class UpstreamTrack:
    artist: str
    title: str
    isrc: str | None = None
    recording_mbid: str | None = None  # upstream named the recording: used as is, not resolved
    release_mbid: str | None = None  # a release of it, when upstream names one


class Upstream(Protocol):
    """An upstream adapter: the playlist `ref` of user `user_id`, in upstream order. Raise on
    failure. An empty list is not applied to a mirror that has tracks (it keeps its last list)."""

    async def tracks(self, user_id: int, ref: str) -> list[UpstreamTrack]: ...


# TrackResolver.resolve: (artist, title, isrc, listenbrainz token)
Resolve = Callable[[str, str, str | None, str | None], Awaitable[RecordingRef | None]]


def waiting(playlist_id: int) -> Select[Any]:
    """Syncs queued, leased, running or waiting to retry."""
    return select(Job).where(
        Job.kind == JobKind.MIRROR_SYNC,
        Job.status.in_(LIVE),
        Job.payload["playlist_id"].as_integer() == playlist_id,
    )


def ensure_sync(s: Session, now: datetime, pl: MirroredPlaylist) -> None:
    """Make sure a sync is queued, due at the playlist's next_sync_at (or now)."""
    if s.scalar(waiting(pl.id).limit(1)) is None:
        due = aware(pl.next_sync_at) if pl.next_sync_at else now
        pl.next_sync_at = due
        enqueue_in(s, now, JobKind.MIRROR_SYNC, {"playlist_id": pl.id}, run_after=due)


def create(
    s: Session, now: datetime, user_id: int, name: str, kind: str, ref: str, cadence_days: int = 1
) -> MirroredPlaylist:
    """For upstream adapters: the user's mirror of upstream (kind, ref), made on the first call
    with its first sync queued now. A name the user already uses raises IntegrityError."""
    pl = s.scalar(
        select(MirroredPlaylist).where(
            MirroredPlaylist.user_id == user_id,
            MirroredPlaylist.upstream_kind == kind,
            MirroredPlaylist.upstream_ref == ref,
        )
    )
    if pl is None:
        pl = MirroredPlaylist(
            user_id=user_id,
            name=name,
            upstream_kind=kind,
            upstream_ref=ref,
            cadence_days=cadence_days,
        )
        s.add(pl)
        s.flush()
        ensure_sync(s, now, pl)
    return pl


def sync_now(s: Session, now: datetime, pl: MirroredPlaylist, force: bool = False) -> bool:
    """Bring the queued sync forward to now. False if synced within the gap (unless `force`)."""
    if not force and pl.last_synced_at and now - aware(pl.last_synced_at) < BUILD_NOW_GAP:
        return False
    pl.next_sync_at = now
    moved = cast(
        CursorResult[Any],
        s.execute(
            update(Job)
            .where(
                Job.kind == JobKind.MIRROR_SYNC,
                Job.status.in_((JobState.QUEUED, JobState.RETRY_WAIT)),
                Job.payload["playlist_id"].as_integer() == pl.id,
            )
            .values(run_after=now, priority=USER_PRIORITY if s.info.get("user") else Job.priority)
        ),
    )
    if moved.rowcount == 0:
        ensure_sync(s, now, pl)
    return True


def track_states(s: Session, pl: MirroredPlaylist) -> list[tuple[dict[str, Any], str]]:
    """Each entry with where it stands. A release with any user's request still in flight is
    not asked for again; once that request ends (failed, or imported without the track) the
    track is missing again."""
    mbids = [e["recording_mbid"] for e in pl.entries if e.get("recording_mbid")]
    releases = [e["release_mbid"] for e in pl.entries if e.get("release_mbid")]
    owned = set(s.scalars(select(Track.recording_mbid).where(Track.recording_mbid.in_(mbids))))
    asked = set(
        s.scalars(
            select(Request.release_mbid).where(
                Request.release_mbid.in_(releases),
                Request.provisional.is_(False),
                Request.status.not_in(DONE),
                # another user's request waiting for approval may never bring the track
                or_(Request.status != "pending_approval", Request.user_id == pl.user_id),
            )
        )
    )
    out: list[tuple[dict[str, Any], str]] = []
    for e in pl.entries:
        m, rel = e.get("recording_mbid"), e.get("release_mbid")
        if not m:
            state = UNRESOLVED
        elif m in owned:
            state = OWNED
        elif not rel:
            state = NO_RELEASE
        else:
            state = REQUESTED if rel in asked else MISSING
        out.append((e, state))
    return out


def request_missing(s: Session, now: datetime, pl: MirroredPlaylist, releases: set[str]) -> int:
    """The owner confirmed: request the missing tracks on `releases` (those the page showed),
    one ordinary request per release. Returns how many tracks were requested. For an
    AUTO_REQUEST kind this first confirmation also turns on auto-requesting for later syncs.
    A NEVER_REQUEST kind raises PermanentError and requests nothing."""
    if pl.upstream_kind in NEVER_REQUEST:
        raise PermanentError(f"{pl.upstream_kind} playlists only list missing tracks")
    if pl.upstream_kind in AUTO_REQUEST and pl.auto_request_since is None:
        pl.auto_request_since = now
    by_release: dict[str, list[str]] = {}
    for e, state in track_states(s, pl):
        if state == MISSING and e["release_mbid"] in releases:
            by_release.setdefault(e["release_mbid"], []).append(e["recording_mbid"])
    for rel, wanted in by_release.items():
        create_request(s, now, pl.user_id, rel, SOURCE, recordings=wanted)
    return sum(len(w) for w in by_release.values())


def key(e: dict[str, Any]) -> tuple[str, str, str | None]:
    return (e["artist"], e["title"], e.get("isrc"))


class Mirror:
    def __init__(
        self, discovery: Discovery, resolve: Resolve, upstreams: dict[str, Upstream]
    ) -> None:
        self.discovery, self.resolve, self.upstreams = discovery, resolve, upstreams

    def handlers(self) -> dict[JobKind, Any]:
        return {JobKind.MIRROR_SYNC: self.sync}

    def start_chains(self) -> None:
        """On startup: every mirrored playlist gets a sync queued if it has none waiting."""
        now = self.discovery.clock.now()
        with self.discovery.sessions.begin() as s:
            for pl in s.scalars(select(MirroredPlaylist)):
                ensure_sync(s, now, pl)

    def _schedule_next(self, s: Session, now: datetime, pl: MirroredPlaylist, job_id: int) -> None:
        """Queue the next sync, keyed on this job so a re-run adds none."""
        if s.scalar(waiting(pl.id).where(Job.id != job_id).limit(1)) is not None:
            return
        cadence = timedelta(days=pl.cadence_days)
        nxt = now + cadence + min(cadence * 0.1, JITTER_CAP) * self.discovery.rng(-1, 1)
        pl.next_sync_at = nxt
        enqueue_in(
            s,
            now,
            JobKind.MIRROR_SYNC,
            {"playlist_id": pl.id},
            run_after=nxt,
            idempotency_key=f"mirror:{pl.id}:after:{job_id}",
        )

    def _stop(self, pid: int, job_id: int, why: str) -> None:
        with self.discovery.sessions.begin() as s:
            if pl := s.get(MirroredPlaylist, pid):
                pl.last_error = why
                self._schedule_next(s, self.discovery.clock.now(), pl, job_id)

    async def sync(self, job: JobRecord) -> None:
        pid = job.payload["playlist_id"]
        try:
            await self._sync(job, pid)
        except Exception:
            # Retries are the queue's; once they run out the chain must go on.
            with self.discovery.sessions.begin() as s:
                if pl := s.get(MirroredPlaylist, pid):
                    self._schedule_next(s, self.discovery.clock.now(), pl, job.id)
            raise

    async def _resolved(
        self, tracks: list[UpstreamTrack], old: list[dict[str, Any]], lb_token: str | None
    ) -> list[dict[str, Any]]:
        """Entries for `tracks`; a track resolved before is not looked up again, an unresolved
        one is retried."""
        known = {key(e): e for e in old if e.get("recording_mbid")}
        by_mbid = {e["recording_mbid"]: e for e in old if e.get("recording_mbid")}
        entries: list[dict[str, Any]] = []
        for t in tracks:
            if t.recording_mbid:
                e = by_mbid.get(t.recording_mbid) or {
                    "recording_mbid": t.recording_mbid,
                    "release_mbid": None,
                    "rec_artist": t.artist,
                    "rec_title": t.title,
                }
                if not e["release_mbid"]:
                    e = {**e, "release_mbid": t.release_mbid}
            else:
                e = known.get((t.artist, t.title, t.isrc))
            if e is None:
                ref = await self.resolve(t.artist, t.title, t.isrc, lb_token)
                e = {
                    "recording_mbid": ref and ref.recording_mbid,
                    "release_mbid": ref and ref.release_mbid,
                    "rec_artist": ref and ref.artist,
                    "rec_title": ref and ref.title,
                }
            entries.append({**e, "artist": t.artist, "title": t.title, "isrc": t.isrc})
        need = list(
            {e["recording_mbid"] for e in entries if e["recording_mbid"] and not e["release_mbid"]}
        )
        if need:  # an ISRC match names no release; requesting needs one
            meta = await self.discovery.history.recording_metadata(need)
            for e in entries:
                if not e["release_mbid"] and (found := meta.get(e["recording_mbid"] or "")):
                    e["release_mbid"] = found.release_mbid
        return entries

    async def _sync(self, job: JobRecord, pid: int) -> None:
        with self.discovery.sessions() as s:
            pl = s.get(MirroredPlaylist, pid)
            if pl is None:
                return
            kind, ref, uid, old = pl.upstream_kind, pl.upstream_ref, pl.user_id, list(pl.entries)
            found, _ = self.discovery.links(s, uid, frozenset())
            lb_token = found["listenbrainz"][1] if found and "listenbrainz" in found else None
        upstream = self.upstreams.get(kind)
        if upstream is None:
            return self._stop(pid, job.id, f"no upstream adapter for {kind!r}")
        try:
            tracks = await upstream.tracks(uid, ref)
        except PermanentError as e:  # no link, unknown user: a retry would not help; say why
            return self._stop(pid, job.id, str(e))
        if not tracks and old:  # likely an adapter hiccup, not a cleared playlist
            return self._stop(pid, job.id, EMPTY_UPSTREAM)
        try:
            entries = await self._resolved(tracks, old, lb_token)
        except PermanentError as e:  # AuthError too: say why instead of failing the job silently
            return self._stop(pid, job.id, str(e))
        before, after = {key(e) for e in old}, {key(e) for e in entries}
        log.info(
            "mirror: playlist %s: %d added, %d removed upstream",
            pid,
            len(after - before),
            len(before - after),
        )
        with self.discovery.sessions.begin() as s:
            pl = s.get(MirroredPlaylist, pid)
            if pl is None:
                return
            pl.entries, pl.last_synced_at = entries, self.discovery.clock.now()
            if pl.auto_request_since is not None:
                # only tracks new upstream: one whose request failed waits for the owner again
                seen = {e.get("recording_mbid") for e in old}
                fresh = {
                    e["release_mbid"]
                    for e, state in track_states(s, pl)
                    if state == MISSING and e["recording_mbid"] not in seen
                }
                if fresh:
                    n = request_missing(s, self.discovery.clock.now(), pl, fresh)
                    log.info("mirror: playlist %s: auto-requested %d new tracks", pid, n)
            links, why = self.discovery.links(s, uid, frozenset({"navidrome"}))
            pl.last_error = why or None
            name, existing = pl.name, pl.navidrome_playlist_id
            self._schedule_next(s, self.discovery.clock.now(), pl, job.id)
            mbids = [e["recording_mbid"] for e in entries if e["recording_mbid"]]
            main = {
                m
                for m in s.scalars(
                    select(Track.recording_mbid).where(Track.recording_mbid.in_(mbids))
                )
                if m
            }
        if links is None:
            return
        await self._push(pid, links["navidrome"], name, existing, entries, main)

    async def _push(
        self,
        pid: int,
        login: tuple[str, str],
        name: str,
        existing: str | None,
        entries: list[dict[str, Any]],
        main: set[str],
    ) -> None:
        """Replace the Navidrome playlist with the owned tracks, in upstream order."""
        d = self.discovery
        lib = d.library_for(*login)
        try:
            folders = await lib.music_folders() if main else {}
            folder = d.folder_id(folders, d.main_library) if main else None
            note = d.missing_note(
                [d.main_library] if main else [], folders, "searching all libraries"
            )
            ids: list[str] = []
            for e in entries:
                m: str | None = e["recording_mbid"]
                if m is None or m not in main:
                    continue  # unresolved or not owned: left out, never guessed
                song = await lib.resolve(m, e["rec_title"], e["rec_artist"], folder)
                if song is not None and song not in ids:
                    ids.append(song)
            if main and not ids:  # Navidrome not scanned yet, or its search is off: keep it
                note, new_id = NONE_FOUND, None
            elif ids or existing:  # no owned tracks empties the playlist mixsync made
                new_id = await lib.push_playlist(f"MixSync: {name}", ids, existing)
            else:
                new_id = None
        except PermanentError as e:  # AuthError, not authorized: retrying would not help
            note, new_id = f"Navidrome: {e}", None
        with d.sessions.begin() as s:
            if (pl := s.get(MirroredPlaylist, pid)) is not None:
                pl.last_error = note
                pl.navidrome_playlist_id = new_id or pl.navidrome_playlist_id
