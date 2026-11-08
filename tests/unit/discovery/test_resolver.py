from collections.abc import Callable
from pathlib import Path

import httpx

from mixsync.core.types import RecordingRef
from mixsync.discovery.listenbrainz import ListenBrainzClient
from mixsync.discovery.resolver import TrackResolver
from mixsync.metadata.musicbrainz import MusicBrainzProvider
from mixsync.ratelimit.client import PoliteClient

Make = Callable[[str, Callable[[httpx.Request], httpx.Response]], PoliteClient]
CASSETTES = Path(__file__).parents[2] / "cassettes"
CLOCKS = RecordingRef("b9ad642e-b012-41c7-b72a-42cf4911f9ff", None, "Coldplay", "Clocks")
LYDIA = RecordingRef(
    "dad4820e-4887-4d1e-83e2-6fe1166b3f49",
    "5828cd29-f48b-40cf-8948-df723d8a87fa",
    "Dauwd",
    "Lydia",
)


def resolver(make: Make, mb_status: int = 200) -> tuple[TrackResolver, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def respond(name: str) -> httpx.Response:
        return httpx.Response(200, content=(CASSETTES / f"{name}.json").read_bytes())

    def mb(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond("musicbrainz/isrc") if mb_status == 200 else httpx.Response(mb_status)

    def lb(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond("listenbrainz/lookup")

    return TrackResolver(
        MusicBrainzProvider(make("musicbrainz", mb)), ListenBrainzClient(make("listenbrainz", lb))
    ), seen


async def test_isrc_resolves_to_the_confident_recording(make_client: Make) -> None:
    r, seen = resolver(make_client)

    assert await r.resolve("Coldplay", "Clocks", "GBAYE0601498") == CLOCKS
    assert [q.url.path for q in seen] == ["/ws/2/isrc/GBAYE0601498"]
    assert seen[0].url.params["inc"] == "artist-credits"


async def test_lookup_resolves_above_the_gate(make_client: Make) -> None:
    r, seen = resolver(make_client)

    assert await r.resolve("Dauwd", "Lydia") == LYDIA
    assert seen[0].url.path == "/1/metadata/lookup/"
    assert seen[0].url.params["artist_name"] == "Dauwd"
    assert seen[0].url.params["recording_name"] == "Lydia"
    assert "authorization" not in seen[0].headers


async def test_below_threshold_lookup_is_unresolved(make_client: Make) -> None:
    r, _ = resolver(make_client)

    assert await r.resolve("Dauwd", "Lydia (Totally Different Song)") is None
    assert await r.resolve("Someone Else", "Lydia") is None


async def test_isrc_without_a_hit_falls_through_to_lookup(make_client: Make) -> None:
    r, seen = resolver(make_client, mb_status=404)

    assert await r.resolve("Dauwd", "Lydia", "XXXXX0000000") == LYDIA
    assert [q.url.path for q in seen] == ["/ws/2/isrc/XXXXX0000000", "/1/metadata/lookup/"]


async def test_isrc_recordings_below_the_gate_fall_through(make_client: Make) -> None:
    r, seen = resolver(make_client)

    assert await r.resolve("Dauwd", "Lydia", "GBAYE0601498") == LYDIA
    assert len(seen) == 2


async def test_isrc_hit_ignores_upstream_version_suffixes(make_client: Make) -> None:
    r, _ = resolver(make_client)

    assert await r.resolve("Coldplay", "Clocks - Remastered 2008", "gbaye-06-01498") == CLOCKS


async def test_malformed_isrc_skips_musicbrainz(make_client: Make) -> None:
    r, seen = resolver(make_client)

    assert await r.resolve("Dauwd", "Lydia", "not/an?isrc") == LYDIA
    assert [q.url.path for q in seen] == ["/1/metadata/lookup/"]


async def test_lookup_miss_is_unresolved(make_client: Make) -> None:
    lb = ListenBrainzClient(make_client("listenbrainz", lambda _: httpx.Response(200, json={})))
    mb = MusicBrainzProvider(make_client("musicbrainz", lambda _: httpx.Response(404)))

    assert await TrackResolver(mb, lb).resolve("Nobody", "Nothing", "GBAYE0601498") is None
