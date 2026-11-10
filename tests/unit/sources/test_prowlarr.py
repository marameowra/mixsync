"""Prowlarr search. The cassettes follow Prowlarr's OpenAPI `ReleaseResource`; they were written
to that schema, not recorded from a live instance."""

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.requests import candidate_dict
from mixsync.core.config import Settings
from mixsync.core.errors import AuthError, PermanentError
from mixsync.core.matching import AlbumInfo, Candidate, Profile, TrackInfo, TransferStatus
from mixsync.match.candidates import rank
from mixsync.ratelimit.client import PoliteClient
from mixsync.sources.prowlarr import (
    MAX_FETCHES,
    ProwlarrSource,
    bdecode,
    info_hash,
    to_candidate,
    torrent_files,
    without_key,
)
from mixsync.sources.qbittorrent import Qbittorrent
from mixsync.sources.sabnzbd import Sabnzbd

CASSETTES = Path(__file__).parents[2] / "cassettes" / "prowlarr"
BASE = "http://prowlarr:9696"
INDEXER = "https://exampleusenet.example/getnzb/4.nzb?i=7&r=INDEXER-KEY"  # Prowlarr's 301 target
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
        if request.url.host == "exampleusenet.example":  # the indexer: never Prowlarr's key
            assert "X-Api-Key" not in request.headers
            return httpx.Response(200, content=(CASSETTES / "album.nzb").read_bytes())
        link = request.url.params["link"]
        if link == "nzb" and not (bodies and link in bodies):  # Usenet always redirects
            return httpx.Response(301, headers={"Location": INDEXER})
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
    assert len(seen) == 5 and clock.now() - datetime(2026, 1, 1, tzinfo=UTC) == timedelta(
        seconds=8
    )  # 1 search + 3 fetches (the NZB's through a redirect) at 1 request / 2 s; the first free


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


ALBUM_HASH = "777fdacdb4a99c43a484126da5682b6b8a0c875c"  # the album.torrent fixture's info hash


def test_info_hash_is_the_sha1_of_the_info_dict_as_written() -> None:
    assert info_hash((CASSETTES / "album.torrent").read_bytes()) == ALBUM_HASH
    for bad in (b"", b"l1:ae", b"d3:abci1ee", b"d4:info"):
        with pytest.raises(ValueError):
            info_hash(bad)


def test_without_key_drops_only_the_api_key() -> None:
    url = "http://prowlarr:9696/1/download?apikey=SECRET&link=a%2Fb&file=Album+1"
    assert without_key(url) == "http://prowlarr:9696/1/download?link=a%2Fb&file=Album+1"


def with_torrents(make_client: Make, added: list[bytes], deleted: list[str]) -> Qbittorrent:
    """A qBittorrent (auth bypassed) that takes every add and reports the album half done."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v2/torrents/add":
            added.append(request.content)
            return httpx.Response(200, text="Ok.")
        if path == "/api/v2/torrents/info":
            row = {"hash": ALBUM_HASH, "progress": 0.5, "state": "downloading", "completed": 1}
            return httpx.Response(200, json=[{**row, "category": "mixsync", "amount_left": 9}])
        if path == "/api/v2/torrents/stop":
            return httpx.Response(200)
        if path == "/api/v2/torrents/delete":
            deleted.append(request.content.decode())
            return httpx.Response(200)
        return httpx.Response(404)

    client = make_client("qbittorrent", handler)
    return Qbittorrent(client, "http://qbittorrent:8080", "", "", Path("/torrents"))


async def test_a_candidate_keeps_its_link_without_the_api_key_and_its_info_hash(
    make_client: Make,
) -> None:
    src = ProwlarrSource(make_client("prowlarr", serve([])), BASE, "REDACTED")
    flac = next(c for c in await src.search(QUERY) if c.folder.endswith("[FLAC]"))
    assert flac.url == f"{BASE}/1/download?link=flac&file=1" and flac.infohash == ALBUM_HASH
    assert "REDACTED" not in json.dumps(candidate_dict(flac))


async def test_enqueue_fetches_the_torrent_again_with_the_key_and_hands_it_to_qbittorrent(
    make_client: Make,
) -> None:
    seen: list[httpx.Request] = []
    added: list[bytes] = []
    torrents = with_torrents(make_client, added, [])
    src = ProwlarrSource(make_client("prowlarr", serve(seen)), BASE, "key-123", torrents)
    flac = next(c for c in await src.search(QUERY) if c.folder.endswith("[FLAC]"))
    seen.clear()
    await src.enqueue(flac)
    (fetch,) = seen
    assert fetch.url.params["apikey"] == "key-123" and fetch.url.params["link"] == "flac"
    assert len(added) == 1 and (CASSETTES / "album.torrent").read_bytes() in added[0]
    assert (await src.status(flac)).state is TransferStatus.IN_PROGRESS


async def test_a_torrent_that_changed_since_the_search_is_never_downloaded(
    make_client: Make,
) -> None:
    added: list[bytes] = []
    other = torrent("Something Else", [["01 - x.flac"]])
    handler = serve([], bodies={"flac": httpx.Response(200, content=other)})
    torrents = with_torrents(make_client, added, [])
    src = ProwlarrSource(make_client("prowlarr", handler), BASE, "k", torrents)
    link = f"{BASE}/1/download?link=flac"
    cand = Candidate("ExampleTracker", "x", (), source="prowlarr", url=link, infohash=ALBUM_HASH)
    with pytest.raises(PermanentError, match="changed since the search"):
        await src.enqueue(cand)
    assert added == []


async def test_usenet_is_skipped_until_a_usenet_client_exists(make_client: Make) -> None:
    seen: list[httpx.Request] = []
    added: list[bytes] = []
    torrents = with_torrents(make_client, added, [])
    only = frozenset({"torrent"})
    src = ProwlarrSource(make_client("prowlarr", serve(seen)), BASE, "k", torrents, only)
    found = await src.search(QUERY)
    assert found and all(c.infohash for c in found) and "nzb" not in fetched(seen)
    nzb = Candidate("ExampleUsenet", "x", (), source="prowlarr", url=f"{BASE}/2/download?link=nzb")
    with pytest.raises(PermanentError):  # never stuck: the pipeline moves to the next candidate
        await src.enqueue(nzb)
    assert added == []


async def test_clear_removes_only_an_unfinished_torrent(make_client: Make) -> None:
    deleted: list[str] = []
    torrents = with_torrents(make_client, [], deleted)
    src = ProwlarrSource(make_client("prowlarr", serve([])), BASE, "k", torrents)
    await src.clear(Candidate("ExampleTracker", "x", (), source="prowlarr", infohash=ALBUM_HASH))
    assert deleted == [f"hashes={ALBUM_HASH}&deleteFiles=true"]


def ours(caplog: pytest.LogCaptureFixture) -> str:
    """MixSync's log lines; httpx's request log is held at WARNING in production."""
    return "\n".join(r.getMessage() for r in caplog.records if r.name != "httpx")


def via_tracker(
    base: Callable[[httpx.Request], httpx.Response], reply: httpx.Response
) -> tuple[Callable[[httpx.Request], httpx.Response], list[httpx.Request]]:
    """`base`, plus a tracker host that answers every request with `reply`."""
    hits: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "tracker.example":
            return base(request)
        hits.append(request)
        assert "X-Api-Key" not in request.headers  # Prowlarr's key stays with Prowlarr
        return reply

    return handler, hits


async def test_a_torrent_redirected_to_the_indexer_is_fetched_there_and_hash_checked(
    make_client: Make, caplog: pytest.LogCaptureFixture
) -> None:
    """Prowlarr 301s a torrent link when the indexer's Redirect is on; the indexer's URL (with
    its passkey) is followed once and never stored or logged."""
    caplog.set_level(logging.DEBUG)
    album = (CASSETTES / "album.torrent").read_bytes()
    redirect = httpx.Response(301, headers={"Location": "https://tracker.example/1?passkey=PASS"})
    tracker = httpx.Response(200, content=album)
    handler, hits = via_tracker(serve([], bodies={"flac": redirect}), tracker)
    added: list[bytes] = []
    torrents = with_torrents(make_client, added, [])
    src = ProwlarrSource(make_client("prowlarr", handler), BASE, "k", torrents)
    flac = next(c for c in await src.search(QUERY) if c.folder.endswith("[FLAC]"))
    assert flac.infohash == ALBUM_HASH and "PASS" not in json.dumps(candidate_dict(flac))
    await src.enqueue(flac)
    assert len(hits) == 2 and album in added[0] and "PASS" not in ours(caplog)
    # the indexer gets a plain User-Agent, never MIXSYNC_MB_CONTACT
    assert all(re.fullmatch(r"MixSync/\S+", r.headers["User-Agent"]) for r in hits)


async def test_a_second_redirect_is_never_followed(make_client: Make) -> None:
    redirect = httpx.Response(301, headers={"Location": "https://tracker.example/1"})
    again = httpx.Response(302, headers={"Location": "https://tracker.example/2"})
    handler, hits = via_tracker(serve([], bodies={"flac": redirect}), again)
    torrents = with_torrents(make_client, [], [])
    src = ProwlarrSource(make_client("prowlarr", handler), BASE, "k", torrents)
    found = await src.search(QUERY)
    assert not [c for c in found if c.folder.endswith("[FLAC]")]
    assert [r.url.path for r in hits] == ["/1"]
    link = f"{BASE}/1/download?link=flac"
    cand = Candidate("ExampleTracker", "x", (), source="prowlarr", url=link, infohash=ALBUM_HASH)
    with pytest.raises(PermanentError, match="302"):
        await src.enqueue(cand)


async def test_an_nzb_redirected_to_the_indexer_goes_to_sabnzbd_as_bytes(
    make_client: Make, caplog: pytest.LogCaptureFixture
) -> None:
    """Usenet results omit their null fields (no seeders); Prowlarr always 301s an NZB link to
    the indexer. SABnzbd gets the NZB's bytes, so neither API key is stored, logged or handed
    to SABnzbd."""
    caplog.set_level(logging.DEBUG)
    calls: list[httpx.Request] = []

    def sab(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if b"addfile" in request.content:
            return httpx.Response(200, json={"status": True, "nzo_ids": ["SABnzbd_nzo_1"]})
        return httpx.Response(200, json={"queue": {"slots": []}, "history": {"slots": []}})

    usenet = Sabnzbd(make_client("sabnzbd", sab), "http://sab:8080", "s", Path("/u"))
    only = frozenset({"usenet"})
    src = ProwlarrSource(make_client("prowlarr", serve([])), BASE, "pkey", None, only, usenet)
    (nzb,) = await src.search(QUERY)
    assert nzb.username == "ExampleUsenet" and nzb.seeders == 0 and nzb.nzb_hash
    assert nzb.url == f"{BASE}/2/download?link=nzb&file=4" and not nzb.infohash
    assert "INDEXER-KEY" not in json.dumps(candidate_dict(nzb))
    await src.enqueue(nzb)
    (add,) = [r for r in calls if b"addfile" in r.content]
    assert (CASSETTES / "album.nzb").read_bytes() in add.content
    assert b"INDEXER-KEY" not in add.content and b"pkey" not in add.content
    assert "INDEXER-KEY" not in ours(caplog)
    assert (await src.status(nzb)).state is TransferStatus.FAILED  # gone from SABnzbd


async def test_an_nzb_that_changed_since_the_search_is_never_downloaded(
    make_client: Make,
) -> None:
    calls: list[httpx.Request] = []

    def sab(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    usenet = Sabnzbd(make_client("sabnzbd", sab), "http://sab:8080", "s", Path("/u"))
    src = ProwlarrSource(make_client("prowlarr", serve([])), BASE, "k", usenet=usenet)
    link = f"{BASE}/2/download?link=nzb"
    cand = Candidate("ExampleUsenet", "x", (), source="prowlarr", url=link, nzb_hash="0" * 40)
    with pytest.raises(PermanentError, match="changed since the search"):
        await src.enqueue(cand)
    assert calls == []
