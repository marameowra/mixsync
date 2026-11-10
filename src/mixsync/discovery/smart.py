"""Smart playlists: rules over the owned library (CONTEXT.md), rebuilt on the playlist's cadence
and pushed to Navidrome, replacing the playlist there. Nothing here requests a track or touches
a file: a track is a candidate only if it has a `tracks` row and Navidrome lists it."""

import logging
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Select, select, update
from sqlalchemy.orm import Session

from mixsync.core.clock import aware
from mixsync.core.errors import PermanentError
from mixsync.core.jobs import LIVE, JobKind, JobState
from mixsync.core.types import LibrarySong
from mixsync.db.models.discovery import SmartPlaylist
from mixsync.db.models.library import Track
from mixsync.db.models.work import Job
from mixsync.db.queue import USER_PRIORITY, JobRecord, enqueue_in
from mixsync.discovery.playlists import BUILD_NOW_GAP, JITTER_CAP, Discovery
from mixsync.watchlist.poller import Rng

log = logging.getLogger(__name__)

SORTS = {"random": "Random", "added": "Recently added first", "year": "Year, oldest first"}
NONE_FOUND = "Navidrome lists none of the owned tracks; skipped push"
Owned = tuple[str, str | None, datetime]  # (recording MBID, year tag, imported_at)
INTS = ("year_from", "year_to", "added_days", "unplayed_days")


def validate_rules(rules: dict[str, Any]) -> None:
    """Raise ValueError unless `rules` is one the smart playlist form could have produced."""
    if unknown := set(rules) - {"genre", "starred", *INTS}:
        raise ValueError(f"unknown rule(s): {', '.join(sorted(unknown))}")
    if "genre" in rules and not (
        isinstance(rules["genre"], str) and 1 <= len(rules["genre"]) <= 100
    ):
        raise ValueError("genre is 1 to 100 characters")
    if "starred" in rules and rules["starred"] is not True:
        raise ValueError("starred is true or absent")
    for key in INTS:
        hi = 9999 if key.startswith("year") else 36500
        if key in rules and not (type(rules[key]) is int and 0 <= rules[key] <= hi):
            raise ValueError(f"{key.replace('_', ' ')} is a whole number from 0 to {hi}")
    if rules.get("year_from", 0) > rules.get("year_to", 9999):
        raise ValueError("the year range must run from the earlier year to the later")


def waiting(playlist_id: int) -> Select[Any]:
    """Builds queued, leased, running or waiting to retry."""
    return select(Job).where(
        Job.kind == JobKind.SMART_BUILD,
        Job.status.in_(LIVE),
        Job.payload["playlist_id"].as_integer() == playlist_id,
    )


def ensure_build(s: Session, now: datetime, pl: SmartPlaylist) -> None:
    """Make sure a build is queued, due at the playlist's next_build_at (or now)."""
    if s.scalar(waiting(pl.id).limit(1)) is None:
        due = aware(pl.next_build_at) if pl.next_build_at else now
        pl.next_build_at = due
        enqueue_in(s, now, JobKind.SMART_BUILD, {"playlist_id": pl.id}, run_after=due)


def build_now(s: Session, now: datetime, pl: SmartPlaylist) -> bool:
    """Bring the queued build forward to now. False if built within the gap."""
    if pl.last_built_at and now - aware(pl.last_built_at) < BUILD_NOW_GAP:
        return False
    pl.next_build_at = now
    moved = cast(
        CursorResult[Any],
        s.execute(
            update(Job)
            .where(
                Job.kind == JobKind.SMART_BUILD,
                Job.status.in_((JobState.QUEUED, JobState.RETRY_WAIT)),
                Job.payload["playlist_id"].as_integer() == pl.id,
            )
            .values(run_after=now, priority=USER_PRIORITY if s.info.get("user") else Job.priority)
        ),
    )
    if moved.rowcount == 0:
        ensure_build(s, now, pl)
    return True


def _matches(
    r: dict[str, Any], song: LibrarySong, year: int | None, added: datetime, now: datetime
) -> bool:
    if r.get("genre") and r["genre"].casefold() not in song.genres:
        return False
    if "year_from" in r and (year is None or year < r["year_from"]):
        return False
    if "year_to" in r and (year is None or year > r["year_to"]):
        return False
    if "added_days" in r and added < now - timedelta(days=r["added_days"]):
        return False
    if r.get("starred") and not song.starred:
        return False
    played = song.played and aware(song.played)
    return not (
        "unplayed_days" in r and played and played > now - timedelta(days=r["unplayed_days"])
    )


def pick(
    owned: list[Owned],
    songs: list[LibrarySong],
    rules: dict[str, Any],
    size: int,
    sort: str,
    now: datetime,
    rng: Rng,
) -> list[LibrarySong]:
    """The owned tracks Navidrome lists that meet every rule, sorted, at most `size`."""
    by_mbid: dict[str, LibrarySong] = {}
    for song in songs:
        if song.recording_mbid:
            by_mbid.setdefault(song.recording_mbid, song)
    hits: dict[str, tuple[LibrarySong, int | None, datetime]] = {}
    for mbid, tag, added in owned:
        song = by_mbid.get(mbid)
        if song is None or mbid in hits:
            continue
        year = int(tag) if tag and tag.isdigit() else song.year  # the file's tag, else Navidrome's
        if _matches(rules, song, year, aware(added), now):
            hits[mbid] = (song, year, aware(added))
    rows = list(hits.values())
    if sort == "added":
        rows.sort(key=lambda h: h[2], reverse=True)
    elif sort == "year":
        rows.sort(key=lambda h: (h[1] is None, h[1] or 0))
    else:
        rows.sort(key=lambda _: rng(0, 1))
    return [song for song, _, _ in rows[:size]]


class Smart:
    def __init__(self, discovery: Discovery) -> None:
        self.d = discovery

    def handlers(self) -> dict[JobKind, Any]:
        return {JobKind.SMART_BUILD: self.build}

    def start_chains(self) -> None:
        """On startup: every smart playlist gets a build queued if it has none waiting."""
        now = self.d.clock.now()
        with self.d.sessions.begin() as s:
            for pl in s.scalars(select(SmartPlaylist)):
                ensure_build(s, now, pl)

    def _schedule_next(self, s: Session, now: datetime, pl: SmartPlaylist, job_id: int) -> None:
        """Queue the next build, keyed on this job so a re-run adds none."""
        if s.scalar(waiting(pl.id).where(Job.id != job_id).limit(1)) is not None:
            return
        cadence = timedelta(days=pl.cadence_days)
        nxt = now + cadence + min(cadence * 0.1, JITTER_CAP) * self.d.rng(-1, 1)
        pl.next_build_at = nxt
        enqueue_in(
            s,
            now,
            JobKind.SMART_BUILD,
            {"playlist_id": pl.id},
            run_after=nxt,
            idempotency_key=f"smart:{pl.id}:after:{job_id}",
        )

    async def build(self, job: JobRecord) -> None:
        pid = job.payload["playlist_id"]
        try:
            await self._build(job, pid)
        except Exception:
            # Retries are the queue's; once they run out the chain must go on.
            with self.d.sessions.begin() as s:
                if pl := s.get(SmartPlaylist, pid):
                    self._schedule_next(s, self.d.clock.now(), pl, job.id)
            raise

    async def _build(self, job: JobRecord, pid: int) -> None:
        d = self.d
        with d.sessions() as s:
            pl = s.get(SmartPlaylist, pid)
            if pl is None:
                return
            links, why = d.links(s, pl.user_id, frozenset({"navidrome"}))
            rules, size, sort = dict(pl.rules), pl.size, pl.sort
            name, existing = pl.name, pl.navidrome_playlist_id
            owned: list[Owned] = [
                (m, y, t)
                for m, y, t in s.execute(
                    select(Track.recording_mbid, Track.year, Track.imported_at)
                    .where(Track.recording_mbid.is_not(None))
                    .order_by(Track.imported_at, Track.id)
                )
                if m
            ]
        picked: list[LibrarySong] | None = None
        new_id: str | None = None
        if links is None:
            note: str | None = why
        else:
            lib = d.library_for(*links["navidrome"])
            try:
                folders = await lib.music_folders()
                note = d.missing_note([d.main_library], folders, "using all libraries")
                songs = await lib.library_songs(d.folder_id(folders, d.main_library))
                listed = {song.recording_mbid for song in songs}
                if owned and not any(m in listed for m, _, _ in owned):
                    note = NONE_FOUND  # Navidrome not scanned yet: keep the last playlist
                else:
                    now = d.clock.now()
                    picked = pick(owned, songs, rules, size, sort, now, d.rng)
                    if picked or existing:  # matching nothing empties the playlist mixsync made
                        ids = [song.id for song in picked]
                        new_id = await lib.push_playlist(f"MixSync: {name}", ids, existing)
            except PermanentError as e:  # AuthError, not authorized: retrying would not help
                note = f"Navidrome: {e}"
        with d.sessions.begin() as s:
            pl = s.get(SmartPlaylist, pid)
            if pl is None:
                return
            now = d.clock.now()
            pl.last_error = note
            if picked is not None:
                pl.entries = [
                    {"recording_mbid": x.recording_mbid, "artist": x.artist, "title": x.title}
                    for x in picked
                ]
                pl.last_built_at = now
                log.info("smart: playlist %s: %d tracks", pid, len(picked))
            pl.navidrome_playlist_id = new_id or pl.navidrome_playlist_id
            self._schedule_next(s, now, pl, job.id)
