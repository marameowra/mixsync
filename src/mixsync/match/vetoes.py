from collections.abc import Sequence

from mixsync.core.matching import FileTrack, TrackInfo

MAX_DURATION_DELTA = 15.0  # seconds


def find_vetoes(pairs: Sequence[tuple[FileTrack, TrackInfo]]) -> tuple[str, ...]:
    """Reasons to force review regardless of the score, for matched (file, track) pairs."""
    out: list[str] = []
    for file, track in pairs:
        if file.length is not None and track.length is not None:
            delta = abs(file.length - track.length)
            if delta > MAX_DURATION_DELTA:
                out.append(f"{file.title!r}: duration off by {delta:.0f}s from {track.title!r}")
        if file.recording_id and file.recording_id != track.recording_id:
            out.append(
                f"{file.title!r}: tagged as recording {file.recording_id}, "
                f"matched to {track.title!r} ({track.recording_id})"
            )
    return tuple(out)
