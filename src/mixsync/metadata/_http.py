from typing import Any

import httpx

from mixsync.core.errors import PermanentError, TransientError
from mixsync.ratelimit.client import PoliteClient


async def get_json(
    client: PoliteClient, url: str, params: dict[str, Any], *, method: str = "GET", **kwargs: Any
) -> Any:
    """GET (or `method`) and decode JSON: network and 5xx are retryable, other failures are not."""
    try:
        response = await client.request(method, url, params=params or None, **kwargs)
    except httpx.TransportError as e:
        raise TransientError(f"{url} unreachable: {e}") from e
    if response.status_code >= 500:
        raise TransientError(f"{url} returned {response.status_code}")
    if response.status_code != 200:
        raise PermanentError(f"{url} returned {response.status_code}")
    return response.json()
