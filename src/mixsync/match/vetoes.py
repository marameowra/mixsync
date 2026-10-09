from collections.abc import Sequence

from mixsync.core.matching import FileTrack, TrackInfo

MAX_DURATION_DELTA = 15.0  # seconds
ACOUSTID_VETO_SCORE = 0.9


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
        best = max(file.acoustid, key=lambda r: r.score, default=None)
        if (
            best
            and best.score >= ACOUSTID_VETO_SCORE
            and best.recording_ids
            and track.recording_id
            and track.recording_id not in best.recording_ids
        ):
            out.append(
                f"{file.title!r}: AcoustID ({best.score:.2f}) says a different recording than "
                f"{track.title!r} ({track.recording_id})"
            )
    return tuple(out)
