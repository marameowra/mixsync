from collections.abc import Callable

import httpx
import pytest

from mixsync.core.errors import TransientError
from mixsync.metadata.coverart import CoverArtClient
from mixsync.ratelimit.client import PoliteClient

Make = Callable[[str, Callable[[httpx.Request], httpx.Response]], PoliteClient]
ORDER = [
    "/release/R/front-1200",
    "/release/R/front",
    "/release-group/G/front-1200",
    "/release-group/G/front",
]


def serve(have: str | None, seen: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.host == "archive.org":
            return httpx.Response(200, content=b"img")
        if request.url.path == have:
            return httpx.Response(307, headers={"Location": "https://archive.org/x.jpg"})
        return httpx.Response(404)

    return handler


@pytest.mark.parametrize("n", range(4))
async def test_chain_falls_through_404s_and_follows_redirect(make_client: Make, n: int) -> None:
    seen: list[str] = []
    art = CoverArtClient(make_client("coverart", serve(ORDER[n], seen)))
    assert await art.front("R", "G") == b"img"
    assert seen[: n + 1] == ORDER[: n + 1]


async def test_nothing_found(make_client: Make) -> None:
    seen: list[str] = []
    art = CoverArtClient(make_client("coverart", serve(None, seen)))
    assert await art.front("R", "G") is None
    assert seen == ORDER
    seen.clear()
    assert await art.front("R", None) is None and seen == ORDER[:2]


async def test_server_error_raises(make_client: Make) -> None:
    art = CoverArtClient(make_client("coverart", lambda _r: httpx.Response(500)))
    with pytest.raises(TransientError):
        await art.front("R", "G")
