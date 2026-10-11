from dataclasses import dataclass


@dataclass(frozen=True)
class Fingerprint:
    duration: float  # seconds
    fingerprint: str


@dataclass(frozen=True)
class AcoustIdResult:
    id: str
    score: float
    recording_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReleaseRef:
    """A search hit: enough to pick a candidate before fetching the full release."""

    release_id: str
    title: str
    artist: str
    date: str | None = None
    country: str | None = None
    track_count: int | None = None
    formats: tuple[str, ...] = ()  # media formats, one per disc
    status: str | None = None


@dataclass(frozen=True)
class ReleaseGroupHit:
    """A release group a watch search found; any of its `release_ids` can be requested."""

    group_id: str
    title: str
    artist: str
    type: str  # album / ep / single / compilation / live / other
    date: str  # YYYY[-MM[-DD]]
    release_ids: tuple[str, ...]


@dataclass(frozen=True)
class EntityRef:
    mbid: str
    name: str
    disambiguation: str = ""
