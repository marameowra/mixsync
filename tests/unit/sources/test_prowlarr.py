"""Prowlarr search. The cassettes follow Prowlarr's OpenAPI `ReleaseResource`; they were written
to that schema, not recorded from a live instance."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.config import Settings
from mixsync.core.errors import AuthError, PermanentError
from mixsync.core.matching import AlbumInfo, Candidate, Profile, TrackInfo
from mixsync.match.candidates import rank
from mixsync.ratelimit.client import PoliteClient
from mixsync.sources.prowlarr import (
    MAX_FETCHES,
    ProwlarrSource,
    bdecode,
    to_candidate,
    torrent_files,
)

CASSETTES = Path(__file__).parents[2] / "cassettes" / "prowlarr"
BASE = "http://prowlarr:9696"
QUERY = "Test Artist Test Album"

Make = Callable[[str, Callable[[httpx.Request], httpx.Response]], PoliteClient]


def bencode(v: Any) -> bytes:
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, str):
        v = v.encode()
    if isinstance(v, bytes):
        return b"%d:%s" % (len(v), v)
    if isinstance(v, list):
        return b"l" + b"".join(bencode(x) for x in v) + b"e"  # pyright: ignore[reportUnknownVariableType]
    return b"d" + b"".join(bencode(k) + bencode(v[k]) for k in sorted(v)) + b"e"


def torrent(name: str, paths: list[list[str]]) -> bytes:
    files = [{"length": 30_000_000, "path": p} for p in paths]
    return bencode({"info": {"name": name, "piece length": 1, "pieces": b"", "files": files}})


class FakeClock:
    def __init__(self) -> None:
        self.t = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


@pytest.fixture
def make_client(settings: Settings, sessions: sessionmaker[Session]) -> Make:
    """The shared fixture with a fake clock, so the 1 request / 2 s bucket costs no real time."""

    def make(service: str, handler: Callable[[httpx.Request], httpx.Response]) -> PoliteClient:
        clock = FakeClock()
        return PoliteClient(
            service,
            settings,
            sessions,
            clock=clock,
            transport=httpx.MockTransport(handler),
            sleep=clock.sleep,
        )

    return make


def search_results() -> list[dict[str, Any]]:
    return json.loads((CASSETTES / "search.json").read_text())


def serve(
    seen: list[httpx.Request],
    results: list[dict[str, Any]] | None = None,
    bodies: dict[str, httpx.Response] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v1/search":
            return httpx.Response(200, json=search_results() if results is None else results)
        link = request.url.params["link"]
        if bodies and link in bodies:
            return bodies[link]
        name = "album.nzb" if link == "nzb" else "album.torrent"
        return httpx.Response(200, content=(CASSETTES / name).read_bytes())

    return handler


def fetched(seen: list[httpx.Request]) -> list[str]:
    return [r.url.params["link"] for r in seen if "link" in r.url.params]


async def test_search_ranks_by_title_then_size_and_fetches_only_the_top(make_client: Make) -> None:
    seen: list[httpx.Request] = []
    src = ProwlarrSource(make_client("prowlarr", serve(seen)), BASE, "key-123")
    found = await src.search(QUERY)
    search = seen[0]
    assert search.headers["X-Api-Key"] == "key-123"
    assert dict(search.url.params) == {"query": QUERY, "type": "search", "categories": "3000"}
    # full title matches first, seeders breaking the tie (mp3 40 > flac 12 > Usenet none); the
    # 8 GB "Test Artist - Test Album" and the better-seeded live album stay unfetched
    assert fetched(seen) == ["mp3", "flac", "nzb"] and len(fetched(seen)) == MAX_FETCHES
    assert [(c.username, c.folder, c.seeders) for c in found] == [
        ("ExampleTracker", "Test Artist - Test Album (2020) [MP3 320]", 40),
        ("ExampleTracker", "Test Artist - Test Album (2020) [FLAC]", 12),
        ("ExampleUsenet", "Test.Artist-Test.Album-WEB-2020-GRP", 0),
    ]
    flac = found[1]
    assert flac.source == "prowlarr" and flac.has_cue
    assert [(f.path, f.size, f.extension) for f in flac.files] == [
        ("Test Artist - Test Album (2020) [FLAC]\\01 - Opening.flac", 30_000_000, "flac"),
        ("Test Artist - Test Album (2020) [FLAC]\\02 - Middle.flac", 31_000_000, "flac"),
        ("Test Artist - Test Album (2020) [FLAC]\\03 - Closer.flac", 29_000_000, "flac"),
    ]
    nzb = found[2]
    assert [(f.path.rpartition("\\")[2], f.size) for f in nzb.files] == [
        ("01 - Opening.flac", 30_600_000),
        ("02 - Middle.flac", 31_600_000),
        ("03 - Closer.flac", 29_600_000),
    ]


async def test_magnet_only_results_are_never_fetched_or_offered(make_client: Make) -> None:
    seen: list[httpx.Request] = []
    magnet = next(r for r in search_results() if r["downloadUrl"] is None)
    assert magnet["magnetUrl"].startswith("magnet:") and magnet["seeders"] == 999
    src = ProwlarrSource(make_client("prowlarr", serve(seen, [magnet])), BASE, "k")
    assert await src.search(QUERY) == [] and fetched(seen) == []


async def test_a_download_that_redirects_to_a_magnet_is_skipped(make_client: Make) -> None:
    seen: list[httpx.Request] = []
    redirect = httpx.Response(301, headers={"Location": "magnet:?xt=urn:btih:00"})
    handler = serve(seen, bodies={"mp3": redirect})
    found = await ProwlarrSource(make_client("prowlarr", handler), BASE, "k").search(QUERY)
    assert [c.folder for c in found] == [
        "Test Artist - Test Album (2020) [FLAC]",
        "Test.Artist-Test.Album-WEB-2020-GRP",
    ]
    assert len(fetched(seen)) == MAX_FETCHES  # the redirect used up one of the fetches


async def test_multi_album_releases_are_rejected_but_disc_folders_are_one_album(
    make_client: Make,
) -> None:
    pack = torrent(
        "Test Artist - Discography",
        [["2018 - First", "01 - A.flac"], ["2020 - Test Album", "01 - Opening.flac"]],
    )
    discs = torrent(
        "Test Artist - Test Album (Deluxe)",
        [["CD1", "01 - Opening.flac"], ["Disc 2", "01 - Bonus.flac"]],
    )
    seen: list[httpx.Request] = []
    rows = [r for r in search_results() if r["id"] in (1, 5)]  # the FLAC and MP3 releases
    handler = serve(
        seen,
        rows,
        {"mp3": httpx.Response(200, content=pack), "flac": httpx.Response(200, content=discs)},
    )
    found = await ProwlarrSource(make_client("prowlarr", handler), BASE, "k").search(QUERY)
    assert [c.folder for c in found] == ["Test Artist - Test Album (2020) [FLAC]"]
    assert len(found[0].files) == 2


async def test_a_link_off_the_prowlarr_host_is_not_fetched(make_client: Make) -> None:
    seen: list[httpx.Request] = []
    row = {**search_results()[0], "downloadUrl": "http://elsewhere.test/1/download?link=x"}
    src = ProwlarrSource(make_client("prowlarr", serve(seen, [row])), BASE, "k")
    assert await src.search(QUERY) == [] and len(seen) == 1  # the API key never left Prowlarr


async def test_the_api_key_in_an_off_host_link_is_never_logged(
    make_client: Make, caplog: pytest.LogCaptureFixture
) -> None:
    row = {**search_results()[0], "downloadUrl": "http://elsewhere.test/1/download?apikey=SECRET"}
    src = ProwlarrSource(make_client("prowlarr", serve([], [row])), BASE, "SECRET")
    await src.search(QUERY)
    assert "elsewhere.test" in caplog.text and "SECRET" not in caplog.text


async def test_a_failed_file_fetch_skips_only_that_release(make_client: Make) -> None:
    seen: list[httpx.Request] = []
    handler = serve(seen, bodies={"mp3": httpx.Response(503)})
    found = await ProwlarrSource(make_client("prowlarr", handler), BASE, "k").search(QUERY)
    assert [c.folder for c in found] == [
        "Test Artist - Test Album (2020) [FLAC]",
        "Test.Artist-Test.Album-WEB-2020-GRP",
    ]


async def test_a_rejected_api_key_is_an_auth_error(make_client: Make) -> None:
    src = ProwlarrSource(make_client("prowlarr", lambda _r: httpx.Response(401)), BASE, "bad")
    with pytest.raises(AuthError, match="MIXSYNC_PROWLARR_API_KEY"):
        await src.search(QUERY)


async def test_downloading_is_refused_until_a_download_client_exists(make_client: Make) -> None:
    src = ProwlarrSource(make_client("prowlarr", lambda _r: httpx.Response(500)), BASE, "k")
    cand = Candidate("ExampleTracker", "x", (), source="prowlarr")
    with pytest.raises(PermanentError):
        await src.enqueue(cand)
    assert await src.active_downloads() == {}


async def test_searches_and_fetches_share_the_prowlarr_bucket(
    settings: Settings, sessions: sessionmaker[Session]
) -> None:
    clock, seen = FakeClock(), list[httpx.Request]()
    client = PoliteClient(
        "prowlarr",
        settings,
        sessions,
        clock=clock,
        transport=httpx.MockTransport(serve(seen)),
        sleep=clock.sleep,
    )
    async with client:
        await ProwlarrSource(client, BASE, "k").search(QUERY)
    assert len(seen) == 4 and clock.now() - datetime(2026, 1, 1, tzinfo=UTC) == timedelta(
        seconds=6
    )  # 1 search + 3 fetches at 1 request / 2 s; the first is free


def test_the_torrent_fixture_decodes() -> None:
    data = (CASSETTES / "album.torrent").read_bytes()
    assert bdecode(data)[b"announce"] == b"http://tracker.example/announce"
    assert len(torrent_files(data)) == 5
    for bad in (b"", b"d3:abc", b"l", b"5:ab", b"x", b"l" * 5000, b"d" * 5000):
        with pytest.raises(ValueError):
            bdecode(bad)
    with pytest.raises(ValueError):
        torrent_files(bencode({"info": {"name": "x", "length": "big"}}))


ALBUM = AlbumInfo(
    "Test Artist",
    "Test Album",
    tuple(TrackInfo(t, length=200, index=i) for i, t in enumerate(["Opening", "Middle"], 1)),
)


def test_rank_keeps_every_release_of_one_indexer_and_breaks_ties_on_seeders() -> None:
    tracks = [["01 - Opening.flac"], ["02 - Middle.flac"]]
    files = torrent_files(torrent("Test Artist - Test Album", tracks))

    def cand(seeders: int) -> Candidate:
        row = {"indexer": "ExampleTracker", "title": "Test Artist - Test Album", "seeders": seeders}
        c = to_candidate(row, files)
        assert c is not None
        return c

    ranked = rank([cand(3), cand(30)], ALBUM, Profile("default", 0.1, 0.4))
    assert [c.seeders for c, _ in ranked] == [30, 3]
    # no upload slot, queue or speed to judge: an indexer release carries no peer penalty
    assert all(p.penalty == 0 for _, r in ranked for p in r.breakdown if p.key == "peer_health")
