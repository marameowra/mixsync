"""Stage 1: score a peer's folder against a MusicBrainz release before downloading anything."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from rapidfuzz import fuzz

from mixsync.core.matching import (
    AlbumInfo,
    Band,
    Candidate,
    CandidateFile,
    FileTrack,
    MatchResult,
    Penalty,
    Profile,
)
from mixsync.match.features import normalize, string_dist
from mixsync.match.profiles import band
from mixsync.match.scorer import (
    SCORER_VERSION,
    TRACK_LENGTH_GRACE,
    TRACK_LENGTH_MAX,
    Distance,
    assign_tracks,
)
from mixsync.match.vetoes import find_vetoes

STAGE1_WEIGHTS: Mapping[str, float] = MappingProxyType(
    {
        "folder_completeness": 12.0,
        "extra_files": 1.0,
        "folder_artist": 6.0,
        "folder_album": 4.0,
        "title_similarity": 3.0,
        "duration_match": 2.0,
        "quality_fit": 1.5,
        "size_plausibility": 2.0,
        "peer_health": 1.5,
    }
)

LOSSLESS = frozenset({"flac", "wav", "ape", "wv", "aiff", "alac"})
MIN_LOSSY_KBPS = 192  # below this a lossy file is a full quality penalty
GOOD_LOSSY_KBPS = 256
FLAC_MIN_KBPS = 400  # smaller than this and the "FLAC" is probably a transcode (SK-Q01)
BITRATE_TOLERANCE = 0.15  # allowed gap between stated and size-implied bitrate (SK-Q02)
LONG_QUEUE = 50
SLOW_SPEED = 50_000  # bytes per second
NO_TITLE_PENALTY = 0.5

_NUMBERED = re.compile(r"^(?:(\d{1,2})-)?(\d{1,3})(?:\s*[-._)]+\s*|\s+|$)(.*)$")
_TRACK_WORD = re.compile(r"^track\s*(\d+)$", re.IGNORECASE)
_DISC_DIR = re.compile(r"^(?:cd|dis[ck])[\s._-]*(\d+)", re.IGNORECASE)


def parse_name(stem: str) -> tuple[int | None, int | None, str]:
    """(disc, track, title) from a file name without extension. `1-01 x` is disc 1, track 1."""
    if m := _TRACK_WORD.match(stem):
        return None, int(m[1]), ""
    for s in (stem, stem.partition(" - ")[2]):  # the second form skips an "Artist - " prefix
        if s and (m := _NUMBERED.match(s)):
            return (int(m[1]) if m[1] else None), int(m[2]), m[3].strip()
    return None, None, stem.strip()


@dataclass(frozen=True)
class _Parsed:
    file: CandidateFile
    name: str
    disc: int | None
    track: int | None
    title: str


def _parse(f: CandidateFile) -> _Parsed:
    folder, _, name = f.path.rpartition("\\")
    disc, track, title = parse_name(name.rpartition(".")[0] or name)
    if (m := _DISC_DIR.match(folder.rpartition("\\")[2])) and disc is None:
        disc = int(m[1])
    return _Parsed(f, name, disc, track, title)


def _quality_key(f: CandidateFile) -> tuple[bool, int, int]:
    return f.extension in LOSSLESS, f.bitrate or 0, f.size


def _dedupe(parsed: Sequence[_Parsed]) -> list[_Parsed]:
    """One file per track, keeping the preferred format (SK-F06)."""
    best: dict[object, _Parsed] = {}
    for p in parsed:
        key = (p.disc, p.track) if p.track is not None else normalize(p.title) or p.file.path
        if key not in best or _quality_key(p.file) > _quality_key(best[key].file):
            best[key] = p
    kept = {id(p) for p in best.values()}
    return [p for p in parsed if id(p) in kept]


def _quality(f: CandidateFile) -> tuple[float, str]:
    if f.extension in LOSSLESS:
        return 0.0, ""
    if f.bitrate is None:
        return 0.5, f"{f.extension} with unknown bitrate"
    if f.bitrate >= GOOD_LOSSY_KBPS:
        return 0.2, f"lossy {f.extension} {f.bitrate} kbps"
    if f.bitrate >= MIN_LOSSY_KBPS:
        return 0.4, f"lossy {f.extension} {f.bitrate} kbps"
    return 1.0, f"{f.extension} {f.bitrate} kbps is below {MIN_LOSSY_KBPS}"


def _size_problem(f: CandidateFile, duration: float | None) -> str:
    """Why the size does not fit the claimed format and bitrate, or "" if it does."""
    if not duration:
        return ""
    implied = f.size * 8 / duration / 1000
    if f.extension == "flac":
        if implied < FLAC_MIN_KBPS:
            return f"flac implies ~{implied:.0f} kbps: likely a lossy transcode"
    elif f.bitrate and abs(implied - f.bitrate) > BITRATE_TOLERANCE * f.bitrate:
        return f"stated {f.bitrate} kbps but size implies ~{implied:.0f} kbps"
    return ""


def _reject(reason: str) -> MatchResult:
    return MatchResult(
        distance=1.0,
        band=Band.REJECT,
        breakdown=(Penalty("unsupported_folder", 1.0, 1.0, reason),),
        vetoes=(reason,),
        scorer_version=SCORER_VERSION,
    )


def score_candidate(
    candidate: Candidate,
    album: AlbumInfo,
    profile: Profile,
    weights: Mapping[str, float] = STAGE1_WEIGHTS,
) -> MatchResult:
    if not candidate.files:
        return _reject("no audio files in the folder")
    if candidate.has_cue and len(candidate.files) == 1 and len(album.tracks) > 1:
        return _reject("single-file album rip with a .cue sheet is not supported")

    parsed = _dedupe([_parse(f) for f in candidate.files])
    tracks = [FileTrack(p.title, "", p.file.duration, p.track, p.disc) for p in parsed]
    by_id = {id(t): p for t, p in zip(tracks, parsed, strict=True)}
    pairs, extras, missing = assign_tracks(tracks, album.tracks)

    dist = Distance(weights)
    vetoes = list(find_vetoes(pairs))
    total = len(album.tracks)
    dist.add(
        "folder_completeness",
        len(missing) / total if total else 0.0,
        f"{total - len(missing)} of {total} tracks present",
    )
    if missing:
        vetoes.append(f"{len(missing)} of {total} tracks missing")
    for e in extras:
        dist.add("extra_files", 1.0, f"{by_id[id(e)].name!r} has no release track")

    where = normalize(candidate.folder)
    if not album.va:
        hit = fuzz.partial_ratio(normalize(album.artist), where) / 100
        dist.add("folder_artist", 1 - hit, f"folder {candidate.folder!r} lacks {album.artist!r}")
    hit = fuzz.partial_ratio(normalize(album.album), where) / 100
    dist.add("folder_album", 1 - hit, f"folder {candidate.folder!r} lacks {album.album!r}")

    untitled = False
    for ft, track in pairs:
        p = by_id[id(ft)]
        if p.title:
            tail = p.title.rpartition(" - ")[2]
            title = min(string_dist(p.title, track.title), string_dist(tail, track.title))
            dist.add("title_similarity", title, f"{p.title!r} vs {track.title!r}")
        else:
            untitled = True
            dist.add("title_similarity", NO_TITLE_PENALTY, f"{p.name!r} has no title")
        if ft.length and track.length:
            delta = abs(ft.length - track.length)
            dist.add_ratio(
                "duration_match",
                delta - TRACK_LENGTH_GRACE,
                TRACK_LENGTH_MAX,
                f"{p.name!r} is {ft.length:.0f}s vs {track.length:.0f}s",
            )
        penalty, why = _quality(p.file)
        dist.add("quality_fit", penalty, f"{p.name!r}: {why}")
        problem = _size_problem(p.file, ft.length or track.length)
        dist.add("size_plausibility", 1.0 if problem else 0.0, f"{p.name!r}: {problem}")
        if problem:
            vetoes.append(f"{p.name!r}: {problem}")
    if untitled:
        vetoes.append("filenames carry no title; matched by position only")

    reasons = [
        (not candidate.has_free_slot, 0.4, "no free upload slot"),
        (candidate.queue_length > LONG_QUEUE, 0.3, f"queue of {candidate.queue_length}"),
        (candidate.upload_speed < SLOW_SPEED, 0.3, f"upload speed {candidate.upload_speed} B/s"),
    ]
    dist.add(
        "peer_health",
        min(1.0, sum(w for bad, w, _ in reasons if bad)),
        "; ".join(r for bad, _, r in reasons if bad),
    )

    return MatchResult(
        distance=dist.distance,
        band=band(dist.distance, vetoes, profile),
        breakdown=dist.breakdown(),
        vetoes=tuple(vetoes),
        scorer_version=SCORER_VERSION,
    )


_ORDER = {Band.AUTO_ACCEPT: 0, Band.REVIEW: 1, Band.REJECT: 2}


def rank(
    candidates: Sequence[Candidate], album: AlbumInfo, profile: Profile
) -> list[tuple[Candidate, MatchResult]]:
    """Best first (ties: free slot, short queue, fast peer), one folder per peer (SK-P08)."""
    scored = sorted(
        ((c, score_candidate(c, album, profile)) for c in candidates),
        key=lambda cr: (
            _ORDER[cr[1].band],
            cr[1].distance,
            not cr[0].has_free_slot,
            cr[0].queue_length,
            -cr[0].upload_speed,
        ),
    )
    seen: set[str] = set()
    out: list[tuple[Candidate, MatchResult]] = []
    for c, r in scored:
        if c.username not in seen:
            seen.add(c.username)
            out.append((c, r))
    return out
