from collections.abc import Sequence

from mixsync.core.matching import FileTrack, TrackInfo
from mixsync.core.types import AcoustIdResult

MAX_DURATION_DELTA = 15.0  # seconds
ACOUSTID_VETO_SCORE = 0.9


def _best(file: FileTrack) -> AcoustIdResult | None:
    best = max(file.acoustid, key=lambda r: r.score, default=None)
    return best if best and best.score >= ACOUSTID_VETO_SCORE and best.recording_ids else None


def acoustid_confirms(file: FileTrack, track: TrackInfo) -> bool:
    """AcoustID's confident best result names the track's recording."""
    best = _best(file)
    return bool(best and track.recording_id in best.recording_ids)


def acoustid_contradicts(file: FileTrack, track: TrackInfo) -> bool:
    """AcoustID is confident the file is a different recording than the track's."""
    best = _best(file)
    return bool(best and track.recording_id and track.recording_id not in best.recording_ids)


def find_vetoes(pairs: Sequence[tuple[FileTrack, TrackInfo]]) -> tuple[str, ...]:
    """Reasons to force review regardless of the score, for matched (file, track) pairs."""
    out: list[str] = []
    for file, track in pairs:
        if file.length is not None and track.length is not None:
            delta = abs(file.length - track.length)
            if delta > MAX_DURATION_DELTA:
                out.append(f"{file.title!r}: duration off by {delta:.0f}s from {track.title!r}")
        # A confident AcoustID match outranks a stale or wrong recording id in the tags.
        if (
            file.recording_id
            and file.recording_id != track.recording_id
            and not acoustid_confirms(file, track)
        ):
            out.append(
                f"{file.title!r}: tagged as recording {file.recording_id}, "
                f"matched to {track.title!r} ({track.recording_id})"
            )
        best = _best(file)
        if best and acoustid_contradicts(file, track):
            out.append(
                f"{file.title!r}: AcoustID ({best.score:.2f}) says a different recording than "
                f"{track.title!r} ({track.recording_id})"
            )
    return tuple(out)


def all_confident(pairs: Sequence[tuple[FileTrack, TrackInfo]]) -> bool:
    """At least one pair, and AcoustID's best result names each file's paired recording."""
    return bool(pairs) and all(acoustid_confirms(f, t) for f, t in pairs)
