import httpx

from mixsync.core.errors import PermanentError, TransientError
from mixsync.ratelimit.client import PoliteClient

_BASE = "https://coverartarchive.org"


class CoverArtClient:
    def __init__(self, client: PoliteClient, base_url: str = _BASE) -> None:
        self._client = client
        self._base = base_url.rstrip("/")

    async def front(self, release_mbid: str, release_group_mbid: str | None) -> bytes | None:
        """Front cover: the release's 1200 px thumbnail, else its original image, then the same
        for the release group. A thumbnail can 404 while the original exists (CAA docs)."""
        entities = [("release", release_mbid)]
        if release_group_mbid:
            entities.append(("release-group", release_group_mbid))
        for kind, mbid in entities:
            for name in ("front-1200", "front"):
                if (data := await self._get(f"{self._base}/{kind}/{mbid}/{name}")) is not None:
                    return data
        return None

    async def _get(self, url: str) -> bytes | None:
        try:
            response = await self._client.get(url)  # redirects to archive.org are followed
        except httpx.TransportError as e:
            raise TransientError(f"{url} unreachable: {e}") from e
        if response.status_code == 404:
            return None
        if response.status_code >= 500:
            raise TransientError(f"{url} returned {response.status_code}")
        if response.status_code != 200:
            raise PermanentError(f"{url} returned {response.status_code}")
        return response.content
