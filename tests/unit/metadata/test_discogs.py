from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from mixsync.core.errors import PermanentError, TransientError
from mixsync.metadata.discogs import DiscogsClient, DiscogsTrack, map_positions
from mixsync.metadata.models import DiscogsTrackRaw
from mixsync.ratelimit.client import PoliteClient

Make = Callable[[str, Callable[[httpx.Request], httpx.Response]], PoliteClient]
CASSETTES = Path(__file__).parents[2] / "cassettes" / "discogs"
TOKEN = "tok-SECRET-123"


def serve(seen: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        name = "identity" if "identity" in path else "search" if "search" in path else "release"
        return httpx.Response(200, text=(CASSETTES / f"{name}.json").read_text())

    return handler


def raw(*rows: str | dict[str, object]) -> list[DiscogsTrackRaw]:
    """A string is a track at that position; a dict is a full row."""
    return [
        DiscogsTrackRaw.model_validate(
            {"position": r, "title": f"t{i}"} if isinstance(r, str) else r
        )
        for i, r in enumerate(rows)
    ]


def pos(tracks: tuple[DiscogsTrack, ...]) -> list[tuple[int, int]]:
    return [(t.disc, t.track) for t in tracks]


def test_plain_numbers_are_one_disc() -> None:
    assert pos(map_positions(raw("1", "2", "3"))) == [(1, 1), (1, 2), (1, 3)]


def test_vinyl_sides_are_numbered_in_order() -> None:
    assert pos(map_positions(raw("A1", "A2", "B1"))) == [(1, 1), (1, 2), (1, 3)]


@pytest.mark.parametrize(
    ("positions", "want"),
    [
        (["1-3"], [(1, 1)]),
        (["2-1", "2-2"], [(2, 1), (2, 2)]),
        (["1-1", "2-1", "1-2"], [(1, 1), (2, 1), (1, 2)]),
        (["CD2-4"], [(2, 1)]),
        (["2.1"], [(2, 1)]),
        (["", ""], [(1, 1), (1, 2)]),
    ],
)
def test_disc_prefix_sets_the_disc_and_tracks_count_within_it(
    positions: list[str], want: list[tuple[int, int]]
) -> None:
    assert pos(map_positions(raw(*positions))) == want


def test_headings_are_dropped_and_index_sub_tracks_count() -> None:
    rows = raw(
        "1",
        {"position": "", "type_": "heading", "title": "Part I"},
        {
            "position": "2",
            "type_": "index",
            "title": "Suite",
            "sub_tracks": [
                {"position": "2a", "title": "Suite a"},
                {"position": "2b", "title": "Suite b"},
            ],
        },
        "3",
    )
    assert [(t.title, t.disc, t.track) for t in map_positions(rows)] == [
        ("t0", 1, 1),
        ("Suite a", 1, 2),
        ("Suite b", 1, 3),
        ("t3", 1, 4),
    ]


def test_index_sub_tracks_take_the_disc_from_their_own_positions() -> None:
    rows = raw(
        "1-1",
        "1-2",
        "2-1",
        {
            "position": "",
            "type_": "index",
            "title": "Suite",
            "sub_tracks": [
                {"position": "2-2a", "title": "Suite a"},
                {"position": "2-2b", "title": "Suite b"},
            ],
        },
        "2-3",
    )
    assert pos(map_positions(rows)) == [(1, 1), (1, 2), (2, 1), (2, 2), (2, 3), (2, 4)]


async def test_non_json_body_is_permanent_and_hides_the_token(make_client: Make) -> None:
    discogs = DiscogsClient(make_client("discogs", lambda _r: httpx.Response(200, text="<html>")))
    with pytest.raises(PermanentError) as e:
        await discogs.release(1, token=TOKEN)
    assert TOKEN not in str(e.value)


async def test_search_releases_sends_the_token_only_as_a_header(make_client: Make) -> None:
    seen: list[httpx.Request] = []
    found = await DiscogsClient(make_client("discogs", serve(seen))).search_releases(
        "Daft Punk", "Discovery", token=TOKEN
    )
    assert [(r.id, r.title, r.artist, r.year, r.track_count) for r in found] == [
        (249504, "Discovery", "Daft Punk", 2001, None),
        (8800, "Discovery (Live)", "Daft Punk", None, None),
    ]
    q = seen[0].url.params
    assert (q["artist"], q["release_title"], q["type"], q["per_page"]) == (
        "Daft Punk",
        "Discovery",
        "release",
        "5",
    )
    assert seen[0].headers["authorization"] == f"Discogs token={TOKEN}"
    assert TOKEN not in str(seen[0].url) and seen[0].headers["user-agent"].startswith("MixSync/")


async def test_search_releases_returns_at_most_five(make_client: Make) -> None:
    hits = ",".join(f'{{"id": {i}, "title": "a - t"}}' for i in range(9))
    discogs = DiscogsClient(
        make_client("discogs", lambda _r: httpx.Response(200, text=f'{{"results": [{hits}]}}'))
    )
    assert len(await discogs.search_releases("a", "t", token=TOKEN)) == 5


async def test_release_has_mapped_tracklist(make_client: Make) -> None:
    seen: list[httpx.Request] = []
    r = await DiscogsClient(make_client("discogs", serve(seen))).release(249504, token=TOKEN)
    assert (r.id, r.title, r.artist, r.year, r.track_count) == (
        249504,
        "Discovery",
        "Daft Punk",
        2001,
        3,
    )
    assert r.tracks == (
        DiscogsTrack("One More Time", "Daft Punk", 1, 1),
        DiscogsTrack("Aerodynamic", "Daft Punk, Guest", 1, 2),
        DiscogsTrack("Disc Two Opener", "Daft Punk", 2, 1),
    )
    assert seen[0].url.path == "/releases/249504"


async def test_username_and_rejected_token(make_client: Make) -> None:
    assert await DiscogsClient(make_client("discogs", serve([]))).username(TOKEN) == "vinylfan"
    bad = DiscogsClient(make_client("discogs", lambda _r: httpx.Response(401)))
    assert await bad.username(TOKEN) is None


async def test_authenticated_requests_are_not_cached(make_client: Make) -> None:
    seen: list[httpx.Request] = []
    discogs = DiscogsClient(make_client("discogs", serve(seen)))
    await discogs.search_releases("a", "b", token=TOKEN)
    await discogs.search_releases("a", "b", token=TOKEN)
    assert len(seen) == 2


@pytest.mark.parametrize(("status", "error"), [(503, TransientError), (404, PermanentError)])
async def test_http_errors(make_client: Make, status: int, error: type[Exception]) -> None:
    discogs = DiscogsClient(make_client("discogs", lambda _r: httpx.Response(status)))
    with pytest.raises(error):
        await discogs.release(1, token=TOKEN)


async def test_timeout_is_transient(make_client: Make) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(TransientError) as e:
        await DiscogsClient(make_client("discogs", slow)).username(TOKEN)
    assert TOKEN not in str(e.value)


async def test_unexpected_shape_is_permanent(make_client: Make) -> None:
    discogs = DiscogsClient(make_client("discogs", lambda _r: httpx.Response(200, text="[]")))
    with pytest.raises(PermanentError):
        await discogs.search_releases("a", "b", token=TOKEN)
