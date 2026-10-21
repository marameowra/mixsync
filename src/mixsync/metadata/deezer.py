# Response shapes checked against https://api.deezer.com on 2026-10-21 (search/album, album,
# album/{id}/tracks). Read-only, no auth; terms in docs/design/services-etiquette.md.
from dataclasses import dataclass

from pydantic import ValidationError

from mixsync.core.errors import PermanentError
from mixsync.metadata._http import get_json
from mixsync.metadata.models import DeezerAlbumRaw, DeezerSearchRaw, DeezerTracksRaw
from mixsync.ratelimit.client import PoliteClient

_BASE = "https://api.deezer.com"
_MAX_ALBUMS = 5
_MAX_TRACKS = 500  # one page: no album is longer


@dataclass(frozen=True)
class DeezerTrack:
    title: str
    artist: str
    disc: int
    track: int


@dataclass(frozen=True)
class DeezerAlbum:
    id: int
    title: str
    artist: str
    year: int | None  # search hits carry no date; only album() fills it
    track_count: int
    tracks: tuple[DeezerTrack, ...] = ()


class DeezerClient:
    def __init__(self, client: PoliteClient, base_url: str = _BASE) -> None:
        self._client = client
        self._base = base_url.rstrip("/")

    async def search_albums(self, artist: str, album: str) -> list[DeezerAlbum]:
        data = await get_json(
            self._client,
            f"{self._base}/search/album",
            {"q": f"{artist} {album}".strip(), "limit": _MAX_ALBUMS},
        )
        try:
            hits = DeezerSearchRaw.model_validate(data).data
        except ValidationError as e:
            raise PermanentError(f"Deezer search returned an unexpected shape: {e}") from e
        return [DeezerAlbum(h.id, h.title, h.artist.name, None, h.nb_tracks) for h in hits][
            :_MAX_ALBUMS
        ]

    async def album(self, album_id: int) -> DeezerAlbum:
        """The album with its tracklist: two requests, since the album lacks disc/track numbers."""
        url = f"{self._base}/album/{album_id}"
        head = await get_json(self._client, url, {})
        tracks = await get_json(self._client, f"{url}/tracks", {"limit": _MAX_TRACKS})
        try:
            a = DeezerAlbumRaw.model_validate(head)
            ts = DeezerTracksRaw.model_validate(tracks).data
        except ValidationError as e:
            raise PermanentError(f"Deezer album {album_id} has an unexpected shape: {e}") from e
        year = int(a.release_date[:4]) if a.release_date[:4].isdecimal() else None
        return DeezerAlbum(
            a.id,
            a.title,
            a.artist.name,
            year,
            a.nb_tracks,
            tuple(DeezerTrack(t.title, t.artist.name, t.disk_number, t.track_position) for t in ts),
        )
