from typing import Any

import httpx

from mixsync.core.errors import PermanentError, TransientError
from mixsync.ratelimit.client import PoliteClient


async def get_json(client: PoliteClient, url: str, params: dict[str, Any]) -> Any:
    """GET and decode JSON: network and 5xx are retryable, other failures are not."""
    try:
        response = await client.get(url, params=params)
    except httpx.TransportError as e:
        raise TransientError(f"{url} unreachable: {e}") from e
    if response.status_code >= 500:
        raise TransientError(f"{url} returned {response.status_code}")
    if response.status_code != 200:
        raise PermanentError(f"{url} returned {response.status_code}")
    return response.json()
