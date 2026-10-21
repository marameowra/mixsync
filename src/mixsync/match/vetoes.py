from collections.abc import Sequence

from mixsync.core.matching import FileTrack, TrackInfo
from mixsync.core.types import AcoustIdResult

MAX_DURATION_DELTA = 15.0  # seconds
ACOUSTID_VETO_SCORE = 0.9
# Share of a lossless file's power above 16 kHz. A lossy source low-passed near 16 kHz leaves
# about 1e-6..1e-5; real masters rarely go below 1e-4. Set low: prefer missing a transcode.
MIN_HIGH_BAND_SHARE = 1e-5
MAX_TRAILING_SILENCE_SHARE = 0.5  # of the file's length: a padded preview is mostly silence


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


def _clock(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def find_vetoes(pairs: Sequence[tuple[FileTrack, TrackInfo]]) -> tuple[str, ...]:
    """Reasons to force review regardless of the score, for matched (file, track) pairs."""
    out: list[str] = []
    for file, track in pairs:
        if file.length is not None and track.length is not None:
            delta = abs(file.length - track.length)
            if delta > MAX_DURATION_DELTA:
                out.append(f"{file.title!r}: duration off by {delta:.0f}s from {track.title!r}")
        if file.high_band is not None and file.high_band < MIN_HIGH_BAND_SHARE:
            out.append(f"{file.title!r}: looks transcoded: almost nothing above 16 kHz")
        if (
            file.trailing_silence is not None
            and file.length
            and file.trailing_silence > MAX_TRAILING_SILENCE_SHARE * file.length
        ):
            out.append(
                f"{file.title!r}: silent from {_clock(file.length - file.trailing_silence)} "
                f"to the end ({_clock(file.length)})"
            )
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
