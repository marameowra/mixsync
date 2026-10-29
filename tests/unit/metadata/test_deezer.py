from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from mixsync.core.errors import PermanentError, TransientError
from mixsync.metadata.deezer import DeezerClient, DeezerTrack
from mixsync.ratelimit.client import PoliteClient

Make = Callable[[str, Callable[[httpx.Request], httpx.Response]], PoliteClient]
CASSETTES = Path(__file__).parents[2] / "cassettes" / "deezer"


def cassette(name: str) -> str:
    return (CASSETTES / f"{name}.json").read_text()


def serve(seen: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        name = "search_album" if path.startswith("/search") else "album"
        return httpx.Response(200, text=cassette(name + ("_tracks" if "tracks" in path else "")))

    return handler


async def test_search_albums(make_client: Make) -> None:
    seen: list[httpx.Request] = []
    deezer = DeezerClient(make_client("deezer", serve(seen)))
    found = await deezer.search_albums("Daft Punk", "Discovery")
    assert [(a.id, a.title, a.artist, a.year, a.track_count) for a in found] == [
        (302127, "Discovery", "Daft Punk", None, 14),
        (999, "Discovery (Live)", "Daft Punk", None, 3),
    ]
    assert seen[0].url.params["q"] == "Daft Punk Discovery" and seen[0].url.params["limit"] == "5"


async def test_search_albums_returns_at_most_five(make_client: Make) -> None:
    hits = ",".join(
        f'{{"id": {i}, "title": "t", "nb_tracks": 1, "artist": {{"name": "a"}}}}' for i in range(9)
    )
    deezer = DeezerClient(
        make_client("deezer", lambda _r: httpx.Response(200, text=f'{{"data": [{hits}]}}'))
    )
    assert len(await deezer.search_albums("a", "t")) == 5


async def test_album_has_tracklist_and_year(make_client: Make) -> None:
    seen: list[httpx.Request] = []
    album = await DeezerClient(make_client("deezer", serve(seen))).album(302127)
    assert (album.id, album.title, album.artist, album.year) == (
        302127,
        "Discovery",
        "Daft Punk",
        2001,
    )
    assert album.tracks[0] == DeezerTrack("One More Time", "Daft Punk", 1, 1)
    assert album.tracks[2] == DeezerTrack("Disc Two Opener", "Daft Punk feat. X", 2, 1)
    assert [r.url.path for r in seen] == ["/album/302127", "/album/302127/tracks"]


async def test_second_search_is_answered_from_the_cache(make_client: Make) -> None:
    seen: list[httpx.Request] = []
    deezer = DeezerClient(make_client("deezer", serve(seen)))
    await deezer.search_albums("a", "b")
    await deezer.search_albums("a", "b")
    assert len(seen) == 1


@pytest.mark.parametrize(("status", "error"), [(503, TransientError), (404, PermanentError)])
async def test_http_errors(make_client: Make, status: int, error: type[Exception]) -> None:
    deezer = DeezerClient(make_client("deezer", lambda _r: httpx.Response(status)))
    with pytest.raises(error):
        await deezer.search_albums("a", "b")


async def test_timeout_is_transient(make_client: Make) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(TransientError):
        await DeezerClient(make_client("deezer", slow)).album(1)


async def test_unexpected_shape_is_permanent(make_client: Make) -> None:
    deezer = DeezerClient(make_client("deezer", lambda _r: httpx.Response(200, text='{"data": 5}')))
    with pytest.raises(PermanentError):
        await deezer.search_albums("a", "b")
