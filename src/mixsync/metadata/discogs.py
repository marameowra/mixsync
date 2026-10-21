# Response shapes from the Discogs API docs (/oauth/identity, /database/search, /releases/{id});
# limits and terms in docs/design/services-etiquette.md. Read-only, per-user personal token.
import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from mixsync.core.errors import PermanentError
from mixsync.metadata._http import get_json
from mixsync.metadata.models import (
    DiscogsIdentityRaw,
    DiscogsReleaseRaw,
    DiscogsSearchRaw,
    DiscogsTrackRaw,
)
from mixsync.ratelimit.client import PoliteClient

_BASE = "https://api.discogs.com"
_MAX_RELEASES = 5
_DISC = re.compile(r"^(?:cd|dvd|disc|disk)?\s*(\d+)\s*[-.]", re.IGNORECASE)
_HOMONYM = re.compile(r"\s\(\d+\)$")  # Discogs tells artists of the same name apart as "Name (2)"


@dataclass(frozen=True)
class DiscogsTrack:
    title: str
    artist: str
    disc: int
    track: int


@dataclass(frozen=True)
class DiscogsRelease:
    id: int
    title: str
    artist: str
    year: int | None
    track_count: int | None  # search hits carry none; only release() fills it
    tracks: tuple[DiscogsTrack, ...] = ()


def map_positions(
    tracklist: list[DiscogsTrackRaw], fallback_artist: str = ""
) -> tuple[DiscogsTrack, ...]:
    """Number tracks within each disc in listed order, so vinyl `A1, A2, B1` becomes 1, 2, 3.

    Headings are dropped; an index track is replaced by its sub-tracks. A `2-`, `2.` or `CD2-`
    position prefix sets the disc (an index's sub-tracks take their disc from the index)."""
    counts: dict[int, int] = {}
    out: list[DiscogsTrack] = []

    def add(t: DiscogsTrackRaw, disc: int) -> None:
        counts[disc] = counts.get(disc, 0) + 1
        artist = ", ".join(_HOMONYM.sub("", a.name) for a in t.artists) or fallback_artist
        out.append(DiscogsTrack(t.title, artist, disc, counts[disc]))

    for t in tracklist:
        m = _DISC.match(t.position.strip())
        disc = int(m.group(1)) if m else 1
        if t.type_ == "track":
            add(t, disc)
        elif t.type_ == "index":
            for sub in t.sub_tracks:
                add(sub, disc)
    return tuple(out)


class DiscogsClient:
    def __init__(self, client: PoliteClient, base_url: str = _BASE) -> None:
        self._client = client
        self._base = base_url.rstrip("/")

    async def _get(self, token: str, path: str, params: dict[str, str | int]) -> Any:
        # the token is only ever a header: never in a URL, log line or error text
        headers = {"Authorization": f"Discogs token={token}"}
        return await get_json(self._client, f"{self._base}{path}", params, headers=headers)

    async def username(self, token: str) -> str | None:
        """The token's owner, or None if Discogs rejects it. Raises TransientError if down."""
        try:
            data = await self._get(token, "/oauth/identity", {})
            return DiscogsIdentityRaw.model_validate(data).username
        except (PermanentError, ValidationError):
            return None

    async def search_releases(self, artist: str, album: str, *, token: str) -> list[DiscogsRelease]:
        params: dict[str, str | int] = {
            "type": "release",
            "artist": artist,
            "release_title": album,
            "per_page": _MAX_RELEASES,
        }
        data = await self._get(token, "/database/search", params)
        try:
            hits = DiscogsSearchRaw.model_validate(data).results
        except ValidationError as e:
            raise PermanentError(f"Discogs search returned an unexpected shape: {e}") from e
        found: list[DiscogsRelease] = []
        for h in hits[:_MAX_RELEASES]:
            who, _, title = h.title.partition(" - ")
            year = int(h.year) if h.year.isdecimal() else None
            found.append(DiscogsRelease(h.id, title or who, _HOMONYM.sub("", who), year, None))
        return found

    async def release(self, release_id: int, *, token: str) -> DiscogsRelease:
        data = await self._get(token, f"/releases/{release_id}", {})
        try:
            r = DiscogsReleaseRaw.model_validate(data)
        except ValidationError as e:
            raise PermanentError(
                f"Discogs release {release_id} has an unexpected shape: {e}"
            ) from e
        artist = ", ".join(_HOMONYM.sub("", a.name) for a in r.artists)
        tracks = map_positions(r.tracklist, artist)
        return DiscogsRelease(r.id, r.title, artist, r.year or None, len(tracks), tracks)
