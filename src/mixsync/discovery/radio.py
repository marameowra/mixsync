"""Radio (CONTEXT.md): a per-user playlist grown by sonic similarity from a seed track, album,
artist or playlist, over the owned, analyzed library at one feature version (ADR 0025). It is
built, rebuilt and pushed by the smart playlist cycle (`smart.Smart`); only the pick is its own.
Until gap filling exists it holds no unowned track, so a build never requests."""

import math
import sys
from collections import Counter
from collections.abc import Collection
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mixsync.core.types import LibrarySong
from mixsync.db.models.discovery import (
    DiscoveryPlaylist,
    MirroredPlaylist,
    RadioPlaylist,
    SmartPlaylist,
)
from mixsync.db.models.library import Track
from mixsync.discovery.smart import Picked, Smart
from mixsync.library.similarity import analyses, nearest

SEED_KINDS = ("track", "album", "artist", "playlist")
PLAYLISTS: dict[str, type[SmartPlaylist] | type[DiscoveryPlaylist] | type[MirroredPlaylist]] = {
    "smart": SmartPlaylist,
    "discovery": DiscoveryPlaylist,
    "mirrored": MirroredPlaylist,
}
MAX_PER_ARTIST = 3  # diversity: a 50-track Radio spans at least 17 artists
MAX_PER_ALBUM = 2
REFUSED = "analyze or acquire this first"
SEED_GONE = f"the seed has no owned, analyzed track; {REFUSED}"


def seed_tracks(s: Session, user_id: int, kind: str, ref: str) -> list[int]:
    """The owned tracks of a seed, or none when it names nothing (or another user's playlist).
    ref: a track id, a release group MBID, an album artist tag, or "<kind>:<id>" of a playlist."""

    def num(v: str) -> int | None:  # a form value; Postgres ids are 32-bit
        return int(v) if v.isascii() and v.isdigit() and len(v) < 10 else None

    if kind == "track":
        where = Track.id == n if (n := num(ref)) is not None else None
    elif kind == "album":
        where = Track.release_group_mbid == ref
    elif kind == "artist":
        where = Track.album_artist == ref
    elif kind == "playlist":
        pk, _, pid = ref.partition(":")
        model = PLAYLISTS.get(pk)
        pl = s.get(model, n) if model and (n := num(pid)) is not None else None
        if pl is None or pl.user_id != user_id:
            return []
        where = Track.recording_mbid.in_(
            [e["recording_mbid"] for e in pl.entries if e.get("recording_mbid")]
        )
    else:
        where = None
    return (
        [] if where is None else list(s.scalars(select(Track.id).where(where).order_by(Track.id)))
    )


def vectors(s: Session, track_ids: Collection[int]) -> dict[int, list[float]]:
    """The similarity vector of each given track analyzed at the current feature version."""
    return {t: a.vector for t, a in analyses(s, track_ids).items() if a.vector}


def check_seed(s: Session, user_id: int, kind: str, ref: str) -> None:
    """Raise ValueError(REFUSED) unless the seed has an owned, analyzed track."""
    if (
        kind not in SEED_KINDS
        or len(ref) > 255  # the seed_ref column
        or not vectors(s, seed_tracks(s, user_id, kind, ref))
    ):
        raise ValueError(REFUSED)


def similarity(distance: float) -> float:
    """1 for the same sound, towards 0 as the euclidean distance between vectors grows."""
    return 1 / (1 + distance)


def pick(
    s: Session, seeds: list[int], lead: bool, size: int, listed: dict[str, LibrarySong]
) -> Picked:
    """Up to `size` owned, analyzed tracks the media server lists (`listed`, by recording),
    closest to the seeds' sound first, at most MAX_PER_ARTIST per artist and MAX_PER_ALBUM per
    album; with `lead` (a one-track seed) the seed track comes first. Each has a reason."""
    seed_vectors = vectors(s, seeds)
    if not seed_vectors:
        return []
    ranked = [(t, similarity(d)) for t, d in nearest(s, seeds, k=sys.maxsize)]
    if lead:
        ranked.insert(0, (seeds[0], 1.0))
    meta = {
        i: (m, (a[0] if a else None) or aa or None, g or ((aa or "", al) if al else None), p)
        for i, m, a, aa, g, al, p in s.execute(
            select(
                Track.id,
                Track.recording_mbid,
                Track.artist_mbids,
                Track.album_artist,
                Track.release_group_mbid,
                Track.album,
                Track.path,
            )
        )
    }
    chosen: list[tuple[int, float, LibrarySong]] = []
    used: set[str] = set()
    artists: Counter[Any] = Counter()
    albums: Counter[Any] = Counter()
    for t, sim in ranked:
        mbid, artist, album, _ = meta.get(t, (None, None, None, ""))
        song = listed.get(mbid) if mbid else None
        if song is None or not mbid or mbid in used:
            continue
        if (artist and artists[artist] >= MAX_PER_ARTIST) or (
            album and albums[album] >= MAX_PER_ALBUM
        ):
            continue
        used.add(mbid)
        artists[artist] += 1
        albums[album] += 1
        chosen.append((t, sim, song))
        if len(chosen) == size:
            break
    picked_vectors = {} if lead else vectors(s, [t for t, _, _ in chosen])

    def name(t: int) -> str:
        mbid, _, _, path = meta[t]
        song = listed.get(mbid) if mbid else None
        return f"{song.artist} - {song.title}" if song else PurePosixPath(path).stem

    out: Picked = []
    for t, sim, song in chosen:
        if lead and t == seeds[0]:
            reason = "the seed track"
        else:
            reason = f"sounds close to the seed ({sim:.2f})"
            if v := picked_vectors.get(t):
                _, closest = min((math.dist(sv, v), k) for k, sv in seed_vectors.items())
                reason += f"; nearest seed track: {name(closest)}"
        out.append((song, {"reason": reason}))
    return out


class Radio(Smart):
    model = RadioPlaylist

    def _read(self, s: Session, pl: Any) -> tuple[list[str], Any] | str:
        seeds = seed_tracks(s, pl.user_id, pl.seed_kind, pl.seed_ref)
        if not vectors(s, seeds):
            return SEED_GONE  # trashed, or analyzed at an older version: keep the last playlist
        owned = select(Track.recording_mbid).where(Track.recording_mbid.is_not(None))
        return [m for m in s.scalars(owned) if m], (seeds, pl.seed_kind == "track", pl.size)

    def _pick(self, state: Any, songs: list[LibrarySong], now: datetime) -> Picked:
        seeds, lead, size = state
        listed: dict[str, LibrarySong] = {}
        for song in songs:
            if song.recording_mbid:
                listed.setdefault(song.recording_mbid, song)
        with self.d.sessions() as s:
            return pick(s, seeds, lead, size, listed)
