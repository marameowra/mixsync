from collections.abc import Sequence

from mixsync.core.matching import FileTrack, TrackInfo
from mixsync.core.types import AcoustIdResult
from mixsync.match.features import normalize

MAX_DURATION_DELTA = 15.0  # seconds
ACOUSTID_VETO_SCORE = 0.9
# Share of a lossless file's power above 16 kHz. A lossy source low-passed near 16 kHz leaves
# about 1e-6..1e-5; real masters rarely go below 1e-4. Set low: prefer missing a transcode.
MIN_HIGH_BAND_SHARE = 1e-5
MAX_TRAILING_SILENCE_SHARE = 0.5  # of the file's length: a padded preview is mostly silence


def confident(file: FileTrack) -> AcoustIdResult | None:
    """AcoustID's best result for the file when it is confident and names recordings."""
    best = max(file.acoustid, key=lambda r: r.score, default=None)
    return best if best and best.score >= ACOUSTID_VETO_SCORE and best.recording_ids else None


def names_recording(best: AcoustIdResult, track: TrackInfo) -> bool:
    """The result names the track's recording: by id, or (MusicBrainz holds duplicate
    recordings of a song) by one with the same normalized title within MAX_DURATION_DELTA."""
    return track.recording_id in best.recording_ids or any(
        r.title is not None
        and r.duration is not None
        and track.length is not None
        and normalize(r.title) == normalize(track.title)
        and abs(r.duration - track.length) <= MAX_DURATION_DELTA
        for r in best.recordings
    )


def acoustid_confirms(file: FileTrack, track: TrackInfo) -> bool:
    """AcoustID's confident best result names the track's recording."""
    best = confident(file)
    return bool(best and names_recording(best, track))


def acoustid_contradicts(file: FileTrack, track: TrackInfo) -> bool:
    """AcoustID is confident the file is a different recording than the track's."""
    best = confident(file)
    return bool(best and track.recording_id and not names_recording(best, track))


def off_release(file: FileTrack, tracks: Sequence[TrackInfo]) -> bool:
    """AcoustID is confident the file is a recording the release (with recording ids) lacks."""
    best = confident(file)
    held = [t for t in tracks if t.recording_id]
    return bool(best and held and not any(names_recording(best, t) for t in held))


def _clock(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def find_vetoes(
    pairs: Sequence[tuple[FileTrack, TrackInfo]],
    extras: Sequence[FileTrack] = (),
    tracks: Sequence[TrackInfo] = (),
) -> tuple[str, ...]:
    """Reasons to force review regardless of the score, for matched (file, track) pairs and the
    extra files AcoustID places off the release `tracks`."""
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
        best = confident(file)
        if best and acoustid_contradicts(file, track):
            out.append(
                f"{file.title!r}: AcoustID ({best.score:.2f}) says a different recording than "
                f"{track.title!r} ({track.recording_id})"
            )
    for file in extras:
        if (best := confident(file)) and off_release(file, tracks):
            ids = ", ".join(best.recording_ids)
            out.append(f"{file.title!r}: release does not contain recording {ids}")
    return tuple(out)


def all_confident(pairs: Sequence[tuple[FileTrack, TrackInfo]]) -> bool:
    """At least one pair, and AcoustID's best result names each file's paired recording."""
    return bool(pairs) and all(acoustid_confirms(f, t) for f, t in pairs)
