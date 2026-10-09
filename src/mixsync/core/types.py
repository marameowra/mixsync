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
