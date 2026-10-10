"""Canonical release policy: which release of a release group the library files tracks under."""

from collections import defaultdict, deque
from collections.abc import Sequence
from pathlib import Path

from mixsync.core.matching import TrackInfo
from mixsync.core.types import ReleaseRef

File = tuple[Path, TrackInfo, str | None]  # (source, track, acoustid_id), as Importer takes them


def _rank(seq: Sequence[str], value: str | None) -> int:
    return seq.index(value) if value in seq else len(seq)  # unlisted = worst


def choose(
    releases: Sequence[ReleaseRef],
    media: Sequence[str] = ("Digital Media", "CD"),
    countries: Sequence[str] = ("XW",),
) -> str:
    """The id of the best release: preferred media, then earliest date, most tracks, preferred
    country, Official status; the id breaks any remaining tie so every worker agrees."""

    def key(r: ReleaseRef) -> tuple[int, bool, str, int, int, bool, str]:
        return (
            min((_rank(media, f) for f in r.formats), default=len(media)),
            not r.date,  # a missing date sorts last; MB dates are YYYY[-MM[-DD]], so text sorts
            r.date or "",
            -(r.track_count or 0),
            _rank(countries, r.country),
            r.status != "Official",
            r.release_id,
        )

    return min(releases, key=key).release_id


def split(files: list[File], canonical: Sequence[TrackInfo]) -> tuple[list[File], list[File]]:
    """Files re-pointed at the canonical release's tracks (by recording MBID), and the rest:
    the bonus tracks. A recording listed twice is given to one file per listing."""
    free: dict[str | None, deque[TrackInfo]] = defaultdict(deque)
    for t in canonical:
        free[t.recording_id].append(t)
    mapped: list[File] = []
    bonus: list[File] = []
    for path, track, acoustid in files:
        if track.recording_id and free[track.recording_id]:
            mapped.append((path, free[track.recording_id].popleft(), acoustid))
        else:
            bonus.append((path, track, acoustid))
    return mapped, bonus
