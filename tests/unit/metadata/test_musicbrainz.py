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
KID_A = "e75c0549-ad55-39e3-8025-c72c5d4a3c5d"
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
    assert album.release_group_id and album.artist_ids == ("a74b1b7f-71a5-4011-9441-d0b5e4122711",)
    assert album.tracks[0].artist_ids == album.artist_ids


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
        "genres",
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


async def test_search_omits_empty_fields_and_skips_an_empty_query(make_client: Make) -> None:
    seen: list[httpx.Request] = []
    body = (MB / "search_releases.json").read_text()
    provider = MusicBrainzProvider(make_client("musicbrainz", serve(body, seen)))
    await provider.search_releases("", "Kid A")
    await provider.search_releases("Radiohead", "")
    assert [r.url.params["query"] for r in seen] == ['release:"Kid A"', 'artist:"Radiohead"']
    assert await provider.search_releases("", "") == []
    assert len(seen) == 2  # no request for the empty one


async def test_error_mapping(make_client: Make) -> None:
    def status(code: int) -> Callable[[httpx.Request], httpx.Response]:
        return lambda _r: httpx.Response(code)

    with pytest.raises(PermanentError):
        await MusicBrainzProvider(make_client("musicbrainz", status(404))).get_release(SINGLE)
    with pytest.raises(TransientError):
        await MusicBrainzProvider(make_client("musicbrainz", status(502))).get_release(SINGLE)


async def test_browse_group_releases_one_request_and_mapping(make_client: Make) -> None:
    seen: list[httpx.Request] = []
    body = (MB / "browse_group_releases.json").read_text()
    refs = await MusicBrainzProvider(
        make_client("musicbrainz", serve(body, seen))
    ).browse_group_releases(KID_A)
    (req,) = seen
    assert req.url.path == "/ws/2/release"
    assert dict(req.url.params) == {
        "release-group": KID_A,
        "inc": "media",
        "fmt": "json",
        "limit": "100",
    }
    digital, vinyl = refs[1], refs[4]
    assert (digital.formats, digital.track_count, digital.status) == (
        ("Digital Media",),
        11,
        "Official",
    )
    assert (digital.date, digital.country) == ("2016-04-01", "XW")
    assert vinyl.formats == ('12" Vinyl', '12" Vinyl') and vinyl.track_count == 10
    assert vinyl.date is None  # MB's "" means unknown
