import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from mixsync.core.errors import PermanentError, TransientError
from mixsync.metadata.musicbrainz import VARIOUS_ARTISTS, MusicBrainzProvider
from mixsync.ratelimit.client import PoliteClient

MB = Path(__file__).parents[2] / "cassettes" / "musicbrainz"
SINGLE = "18e2b05b-937a-3c25-ac78-07267e6411a0"
MULTI = "4393b20c-f635-491e-92c0-2a5519151e5d"

Make = Callable[[str, Callable[[httpx.Request], httpx.Response]], PoliteClient]


def serve(body: str, seen: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=body)

    return handler


async def test_multi_disc_mapping(make_client: Make) -> None:
    seen: list[httpx.Request] = []
    body = (MB / "release_multi_disc.json").read_text()
    album = await MusicBrainzProvider(make_client("musicbrainz", serve(body, seen))).get_release(
        MULTI
    )
    assert (album.artist, album.album, album.year, album.country) == (
        "Radiohead",
        "KID A MNESIA",
        2021,
        "GB",
    )
    assert album.original_year == 2021 and album.mediums == 3 and not album.va
    assert album.label == "XL Recordings" and album.catalognum == "XL1166LP"
    assert [t.index for t in album.tracks] == list(range(1, 34))
    assert [(t.medium, t.medium_index) for t in album.tracks[9:12]] == [(1, 10), (2, 1), (2, 2)]
    # the track has length None, so the recording's length is used, converted ms -> s
    assert album.tracks[0].length == pytest.approx(251.426)
    assert album.tracks[0].recording_id == "60bd9d53-01ff-4562-8058-eb44b3940317"


async def test_single_disc_request_and_mapping(make_client: Make) -> None:
    seen: list[httpx.Request] = []
    body = (MB / "release_single_disc.json").read_text()
    album = await MusicBrainzProvider(make_client("musicbrainz", serve(body, seen))).get_release(
        SINGLE
    )
    assert album.original_year == 2000 and album.year == 2000 and album.media == "CD"
    assert [(t.medium, t.medium_index) for t in album.tracks[:2]] == [(1, 1), (1, 2)]
    (req,) = seen
    assert req.url.path == f"/ws/2/release/{SINGLE}"
    assert req.url.params["fmt"] == "json"
    assert set(req.url.params["inc"].split(" ")) == {
        "recordings",
        "artist-credits",
        "media",
        "labels",
        "release-groups",
    }
    assert req.headers["user-agent"].startswith("MixSync/")


async def test_va_flag(make_client: Make) -> None:
    data = json.loads((MB / "release_single_disc.json").read_text())
    data["artist-credit"][0]["artist"]["id"] = VARIOUS_ARTISTS
    album = await MusicBrainzProvider(
        make_client("musicbrainz", serve(json.dumps(data), []))
    ).get_release(SINGLE)
    assert album.va


async def test_search(make_client: Make) -> None:
    seen: list[httpx.Request] = []
    body = (MB / "search_releases.json").read_text()
    provider = MusicBrainzProvider(
        make_client("musicbrainz", serve(body, seen)), "http://mirror.test/"
    )
    refs = await provider.search_releases("Radiohead", 'Kid "A"', limit=3)
    assert (refs[0].release_id, refs[0].title, refs[0].artist) == (SINGLE, "Kid A", "Radiohead")
    assert (refs[0].date, refs[0].country, refs[0].track_count) == ("2000-10-03", "US", 10)
    (req,) = seen
    assert req.url.host == "mirror.test"
    assert req.url.params["query"] == 'release:"Kid \\"A\\"" AND artist:"Radiohead"'
    assert (req.url.params["limit"], req.url.params["fmt"]) == ("3", "json")


async def test_error_mapping(make_client: Make) -> None:
    def status(code: int) -> Callable[[httpx.Request], httpx.Response]:
        return lambda _r: httpx.Response(code)

    with pytest.raises(PermanentError):
        await MusicBrainzProvider(make_client("musicbrainz", status(404))).get_release(SINGLE)
    with pytest.raises(TransientError):
        await MusicBrainzProvider(make_client("musicbrainz", status(502))).get_release(SINGLE)
