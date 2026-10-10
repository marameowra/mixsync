import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from mixsync.metadata.musicbrainz import MusicBrainzProvider
from mixsync.ratelimit.client import PoliteClient

FIXTURE = Path(__file__).parents[2] / "cassettes" / "musicbrainz" / "release_genres.json"
Make = Callable[[str, Callable[[httpx.Request], httpx.Response]], PoliteClient]


async def genres(
    make_client: Make, mbid: str, edit: Callable[[dict[str, Any]], None]
) -> tuple[str, ...]:
    data = json.loads(FIXTURE.read_text())
    edit(data)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=data)

    album = await MusicBrainzProvider(make_client("musicbrainz", handler)).get_release(mbid)
    assert "genres" in seen[0].url.params["inc"].split()
    return album.genres


def no_group(d: dict[str, Any]) -> None:
    d["release-group"]["genres"] = []


def no_group_or_release(d: dict[str, Any]) -> None:
    no_group(d)
    d["genres"] = []


def nothing(d: dict[str, Any]) -> None:
    no_group_or_release(d)
    d["artist-credit"][0]["artist"]["genres"] = []


async def test_release_group_ranked_by_votes_then_name(make_client: Make) -> None:
    got = await genres(make_client, "a", lambda d: None)
    assert got == ("art rock", "electronic", "rock", "ambient")  # tie at 11 -> alphabetical


async def test_falls_back_to_release_then_artist(make_client: Make) -> None:
    assert await genres(make_client, "b", no_group) == ("electronic",)
    assert await genres(make_client, "c", no_group_or_release) == ("alternative rock", "rock")


async def test_no_genres_anywhere(make_client: Make) -> None:
    assert await genres(make_client, "d", nothing) == ()
