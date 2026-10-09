from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True)
class TrackInfo:
    """A track on a MusicBrainz release."""

    title: str
    artist: str | None = None
    length: float | None = None  # seconds
    index: int | None = None  # position across the whole release
    medium_index: int | None = None  # position within its disc
    medium: int | None = None  # disc number
    recording_id: str | None = None


@dataclass(frozen=True)
class AlbumInfo:
    """A MusicBrainz release."""

    artist: str
    album: str
    tracks: tuple[TrackInfo, ...]
    va: bool = False
    year: int | None = None
    original_year: int | None = None
    country: str | None = None
    label: str | None = None
    catalognum: str | None = None
    albumdisambig: str | None = None
    media: str | None = None
    mediums: int | None = None
    release_id: str | None = None


@dataclass(frozen=True)
class FileTrack:
    """What a downloaded file's tags and audio say about it."""

    title: str
    artist: str
    length: float | None = None  # seconds
    track: int | None = None
    disc: int | None = None
    recording_id: str | None = None


@dataclass(frozen=True)
class Likelies:
    """Album-level tag consensus across the files of a batch."""

    artist: str | None = None
    album: str | None = None
    year: int | None = None
    disctotal: int | None = None
    country: str | None = None
    label: str | None = None
    catalognum: str | None = None
    albumdisambig: str | None = None
    release_id: str | None = None
    media: str | None = None


class Band(StrEnum):
    AUTO_ACCEPT = "auto_accept"
    REVIEW = "review"
    REJECT = "reject"


@dataclass(frozen=True)
class Profile:
    name: str
    auto_accept_max: float  # distance <= this auto-accepts
    review_max: float  # distance <= this goes to review, above rejects


@dataclass(frozen=True)
class Penalty:
    """One scored feature. Its share of the distance is penalty * weight / total weight."""

    key: str
    penalty: float  # mean of the raw penalties for this key, in [0, 1]
    weight: float  # configured weight times the number of penalties for this key
    reason: str


@dataclass(frozen=True)
class MatchResult:
    distance: float
    band: Band
    breakdown: tuple[Penalty, ...]
    vetoes: tuple[str, ...]
    scorer_version: int
