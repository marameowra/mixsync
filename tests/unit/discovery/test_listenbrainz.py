from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.config import Settings
from mixsync.core.errors import AuthError, PermanentError, TransientError
from mixsync.core.types import RecordingRef
from mixsync.db.models.infra import RateBucket
from mixsync.discovery.listenbrainz import BATCH, LOVES_CAP, LOVES_PAGE, ListenBrainzClient
from mixsync.ratelimit.client import PoliteClient
from mixsync.ratelimit.services import user_agent

Make = Callable[[str, Callable[[httpx.Request], httpx.Response]], PoliteClient]
CASSETTES = Path(__file__).parents[2] / "cassettes" / "listenbrainz"
TOKEN = "s3cret-token-value"
M1 = "dad4820e-4887-4d1e-83e2-6fe1166b3f49"
M2 = "0fffc332-04c6-4619-aa9a-3d043c447612"
REF1 = RecordingRef(M1, "5828cd29-f48b-40cf-8948-df723d8a87fa", "Dauwd", "Lydia")


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

    assert refs == [REF1]  # the metadata cassette knows only the first recording
    rec, meta = seen
    assert rec.url.path == "/1/cf/recommendation/user/unclejohn69/recording"
    assert rec.url.params["count"] == "2"
    assert meta.url.path == "/1/metadata/recording/"
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
        "/1/playlist/55f2a01f-3974-46ca-8461-a8a84886eba4",
    ]
    assert [(r.recording_mbid, r.release_mbid, r.artist, r.title) for r in refs][:2] == [
        (M1, "5828cd29-f48b-40cf-8948-df723d8a87fa", "Dauwd", "Lydia"),
        (M2, "b7c55d9b-229b-446b-ae1a-6793aea289c4", "Rival Consoles", "Theme (From MindsEye)"),
    ]
    assert len(refs) == 3


async def test_weekly_exploration_empty_when_none(make_client: Make) -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"playlists": [], "count": 0})

    assert await lb(make_client, handler).weekly_exploration("u", TOKEN) == []


async def test_weekly_exploration_falls_back_to_title(make_client: Make) -> None:
    listing = {
        "playlists": [
            {
                "playlist": {
                    "identifier": "https://listenbrainz.org/playlist/p1",
                    "title": "Weekly Exploration for u",
                }
            }
        ]
    }
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200, json={"playlist": {"track": []}} if "/p1" in paths[-1] else listing
        )

    assert await lb(make_client, handler).weekly_exploration("u", TOKEN) == []
    assert paths[-1] == "/1/playlist/p1"


async def test_weekly_exploration_beyond_the_default_page(make_client: Make) -> None:
    others = [
        {
            "playlist": {
                "identifier": f"https://x/playlist/o{i}",
                "title": "Daily Jams",
                "date": "2026-10-11",
            }
        }
        for i in range(12)
    ]
    weekly = {
        "playlist": {
            "identifier": "https://x/playlist/we",
            "title": "Weekly Exploration for u",
            "date": "2026-08-01",
        }
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "/playlist/we" in request.url.path:
            return cassette("playlist")
        return httpx.Response(200, json={"playlists": [*others, weekly]})

    refs = await lb(make_client, handler).weekly_exploration("u", TOKEN)

    assert seen[0].url.params["count"] == "100"
    assert seen[1].url.path == "/1/playlist/we"
    assert len(refs) == 3


async def test_playlists_without_identifier_are_skipped(make_client: Make) -> None:
    listing = {"playlists": [{"playlist": {"title": "Weekly Exploration for u"}}]}
    client = lb(make_client, lambda _r: httpx.Response(200, json=listing))
    assert await client.weekly_exploration("u", TOKEN) == []


async def test_null_metadata_fields_do_not_fail_the_call(make_client: Make) -> None:
    body = {
        M1: {"recording": {"name": None}, "release": None, "artist": None},
        M2: {"recording": None, "release": {"mbid": None}, "artist": {"name": None}},
    }
    found = await lb(make_client, lambda _r: httpx.Response(200, json=body)).recording_metadata(
        [M1, M2]
    )
    assert found == {M1: RecordingRef(M1, None, "", ""), M2: RecordingRef(M2, None, "", "")}


async def test_usernames_are_percent_encoded_in_paths(make_client: Make) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    client = lb(make_client, handler)
    await client.recommendations("a/b?c", TOKEN, 1)
    await client.weekly_exploration("a/b?c", TOKEN)

    assert [r.url.raw_path.split(b"?")[0] for r in seen] == [
        b"/1/cf/recommendation/user/a%2Fb%3Fc/recording",
        b"/1/user/a%2Fb%3Fc/playlists/createdfor",
    ]


async def test_track_identifier_and_release_hint_edge_cases(make_client: Make) -> None:
    track = {"title": "T", "creator": "A"}
    body = {
        "payload": {
            "jspf": {
                "playlist": {
                    "track": [
                        {
                            **track,
                            "identifier": ["https://musicbrainz.org/recording/" + M1, "x/" + M2],
                        },
                        {**track, "identifier": "https://musicbrainz.org/recording/" + M2},
                    ]
                }
            }
        }
    }
    refs = await lb(make_client, lambda _r: httpx.Response(200, json=body)).lb_radio(
        "p", "easy", TOKEN
    )
    assert refs == [RecordingRef(M1, None, "A", "T"), RecordingRef(M2, None, "A", "T")]


async def test_redirect_is_a_clear_permanent_error(make_client: Make) -> None:
    client = lb(make_client, lambda _r: httpx.Response(308, headers={"Location": "/x/"}))
    with pytest.raises(PermanentError, match="redirected"):
        await client.recording_metadata([M1])


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
    assert found == {M1: REF1}


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


async def test_loves_newest_first_with_metadata_and_no_token(
    make_client: Make, settings: Settings, sessions: sessionmaker[Session]
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return cassette("feedback_loves")

    client = lb(make_client, handler)
    refs = await client.loves("uncle john")

    assert refs == [  # the love with no recording MBID is left out
        REF1,
        RecordingRef(M2, None, "Rival Consoles", "Theme (From MindsEye)"),
    ]
    (req,) = seen
    assert req.url.raw_path.startswith(b"/1/feedback/user/uncle%20john/get-feedback?")
    assert dict(req.url.params) == {
        "score": "1",
        "count": str(LOVES_PAGE),
        "offset": "0",
        "metadata": "true",
    }
    assert "authorization" not in req.headers  # public, so the response cache applies
    assert req.headers["user-agent"] == user_agent(settings)
    with sessions() as s:
        assert s.get(RateBucket, "listenbrainz") is not None  # went through the bucket
    assert await client.loves("uncle john") == refs
    assert len(seen) == 1  # served from the cache


async def test_loves_paginate_up_to_the_cap_and_sort_newest_first(make_client: Make) -> None:
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        off = int(request.url.params["offset"])
        offsets.append(off)
        # oldest first, to show the client does not rely on the server's order
        page = [
            {"created": i, "recording_mbid": f"m{i}"}
            for i in range(off, min(off + LOVES_PAGE, LOVES_CAP + 50))
        ]
        return httpx.Response(200, json={"feedback": page})

    refs = await lb(make_client, handler).loves("u")
    assert offsets == list(range(0, LOVES_CAP, LOVES_PAGE))  # stops at the cap
    assert len(refs) == LOVES_CAP
    assert refs[0].recording_mbid == f"m{LOVES_CAP - 1}"


async def test_loves_stop_on_a_short_page(make_client: Make) -> None:
    offsets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offsets.append(request.url.params["offset"])
        n = LOVES_PAGE if len(offsets) == 1 else 1
        page = [{"created": 1, "recording_mbid": f"m{i}"} for i in range(n)]
        return httpx.Response(200, json={"feedback": page})

    assert len(await lb(make_client, handler).loves("u")) == LOVES_PAGE + 1
    assert offsets == ["0", str(LOVES_PAGE)]


async def test_loves_failure_raises(make_client: Make) -> None:
    with pytest.raises(TransientError):
        await lb(make_client, lambda _r: httpx.Response(503)).loves("u")


async def test_lookup_sends_the_token(make_client: Make) -> None:
    seen: list[httpx.Request] = []

    def respond(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return cassette("lookup")

    assert await lb(make_client, respond).lookup("Dauwd", "Lydia", TOKEN) == REF1
    assert seen[0].headers["authorization"] == f"Token {TOKEN}"


async def test_a_401_without_a_token_says_one_was_required(make_client: Make) -> None:
    client = lb(make_client, lambda _r: httpx.Response(401))

    with pytest.raises(AuthError, match="required a token"):
        await client.loves("someone")
    with pytest.raises(AuthError, match="rejected the user token") as info:
        await client.lookup("a", "b", TOKEN)
    assert TOKEN not in str(info.value)
