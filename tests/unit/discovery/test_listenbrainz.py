from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from mixsync.core.errors import AuthError, PermanentError, TransientError
from mixsync.core.types import RecordingRef
from mixsync.discovery.listenbrainz import BATCH, ListenBrainzClient
from mixsync.ratelimit.client import PoliteClient

Make = Callable[[str, Callable[[httpx.Request], httpx.Response]], PoliteClient]
CASSETTES = Path(__file__).parents[2] / "cassettes" / "listenbrainz"
TOKEN = "s3cret-token-value"
M1 = "526bd613-fddd-4bd6-9137-ab709ac74cab"
M2 = "a6081bc1-2a76-4984-b21f-38bc3dcca3a5"
REF1 = RecordingRef(M1, "11111111-1111-1111-1111-111111111111", "Artist One", "Song One")
REF2 = RecordingRef(M2, "22222222-2222-2222-2222-222222222222", "Artist Two", "Song Two")


def cassette(name: str) -> httpx.Response:
    return httpx.Response(200, content=(CASSETTES / f"{name}.json").read_bytes())


def lb(make: Make, handler: Callable[[httpx.Request], httpx.Response]) -> ListenBrainzClient:
    return ListenBrainzClient(make("listenbrainz", handler))


async def test_recommendations_look_up_metadata(make_client: Make) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return cassette("metadata" if "metadata" in request.url.path else "recommendations")

    refs = await lb(make_client, handler).recommendations("unclejohn69", TOKEN, 2)

    assert refs == [REF1, REF2]
    rec, meta = seen
    assert rec.url.path == "/1/cf/recommendation/user/unclejohn69/recording"
    assert rec.url.params["count"] == "2"
    assert meta.url.params["recording_mbids"] == f"{M1},{M2}"
    assert meta.url.params["inc"] == "release artist"
    assert "authorization" not in meta.headers


async def test_weekly_exploration_picks_newest_weekly_playlist(make_client: Make) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return cassette("playlist" if "/playlist/" in request.url.path else "createdfor")

    refs = await lb(make_client, handler).weekly_exploration("unclejohn69", TOKEN)

    assert paths == [
        "/1/user/unclejohn69/playlists/createdfor",
        "/1/playlist/aaaaaaaa-0000-0000-0000-000000000002",
    ]
    assert [(r.recording_mbid, r.artist, r.title) for r in refs] == [
        (M1, "Artist One", "Song One"),
        (M2, "Artist Two", "Song Two"),
    ]


async def test_weekly_exploration_empty_when_none(make_client: Make) -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"playlists": [], "count": 0})

    assert await lb(make_client, handler).weekly_exploration("u", TOKEN) == []


async def test_lb_radio(make_client: Make) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return cassette("lb_radio")

    refs = await lb(make_client, handler).lb_radio("artist:(Artist One)", "easy", TOKEN)

    assert [r.recording_mbid for r in refs] == [M1]
    assert seen[0].url.path == "/1/explore/lb-radio"
    assert dict(seen[0].url.params) == {"prompt": "artist:(Artist One)", "mode": "easy"}
    assert seen[0].headers["authorization"] == f"Token {TOKEN}"


async def test_204_means_empty(make_client: Make) -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    client = lb(make_client, handler)
    assert await client.recommendations("u", TOKEN, 10) == []
    assert await client.weekly_exploration("u", TOKEN) == []
    assert await client.lb_radio("p", "easy", TOKEN) == []
    assert await client.recording_metadata([M1]) == {}


async def test_metadata_is_chunked_and_unauthenticated(make_client: Make) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return cassette("metadata")

    mbids = [f"{i:08d}-0000-0000-0000-000000000000" for i in range(BATCH * 2 + 1)]
    found = await lb(make_client, handler).recording_metadata(mbids)

    assert [len(r.url.params["recording_mbids"].split(",")) for r in seen] == [BATCH, BATCH, 1]
    assert all("authorization" not in r.headers for r in seen)
    assert found == {M1: REF1, M2: REF2}


async def test_validate_token(make_client: Make) -> None:
    seen: list[httpx.Request] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return cassette("validate_ok")

    assert await lb(make_client, ok).validate_token(TOKEN) == "unclejohn69"
    assert seen[0].url.path == "/1/validate-token"
    assert seen[0].headers["authorization"] == f"Token {TOKEN}"
    assert TOKEN not in str(seen[0].url)

    assert await lb(make_client, lambda _r: cassette("validate_bad")).validate_token("x") is None
    assert await lb(make_client, lambda _r: httpx.Response(401)).validate_token("x") is None


async def test_user_agent_is_always_set(make_client: Make) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return cassette("validate_ok")

    await lb(make_client, handler).validate_token(TOKEN)
    assert seen[0].headers["user-agent"].startswith("MixSync/")


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, AuthError),
        (404, PermanentError),
        (400, PermanentError),
        (500, TransientError),
        (502, TransientError),
    ],
)
async def test_error_mapping_never_leaks_the_token(
    make_client: Make, status: int, error: type[Exception]
) -> None:
    client = lb(make_client, lambda _r: httpx.Response(status, text=f"bad {TOKEN}"))

    with pytest.raises(error) as info:
        await client.lb_radio("p", "easy", TOKEN)

    assert TOKEN not in str(info.value)
    assert (status == 401) == isinstance(info.value, AuthError)


async def test_transport_error_is_transient_and_hides_the_token(make_client: Make) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom {TOKEN}", request=request)

    with pytest.raises(TransientError) as info:
        await lb(make_client, handler).lb_radio("p", "easy", TOKEN)
    assert TOKEN not in str(info.value)


async def test_unusable_body_is_permanent(make_client: Make) -> None:
    client = lb(make_client, lambda _r: httpx.Response(200, text="<html>"))
    with pytest.raises(PermanentError):
        await client.lb_radio("p", "easy", TOKEN)
