import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from mixsync.core.errors import PermanentError, TransientError
from mixsync.core.matching import Candidate, CandidateFile, TransferStatus
from mixsync.ratelimit.client import PoliteClient
from mixsync.sources.slskd import SlskdSource, map_responses

FX = Path(__file__).parents[2] / "fixtures" / "slskd"
BASE = "http://slskd:5030"

Make = Callable[[str, Callable[[httpx.Request], httpx.Response]], PoliteClient]


def load(name: str) -> object:
    return json.loads((FX / name).read_text())


async def _no_sleep(_s: float) -> None:
    return None


def source(make_client: Make, handler: Callable[[httpx.Request], httpx.Response]) -> SlskdSource:
    return SlskdSource(make_client("slskd", handler), BASE, "key-123", sleep=_no_sleep)


def test_mapping_groups_folders_and_drops_non_audio() -> None:
    cands = map_responses(load("responses.json"))  # pyright: ignore[reportArgumentType]
    by_folder = {(c.username, c.folder): c for c in cands}
    # peer-001: SK-F03, five non-audio files dropped, the lone .cue is remembered
    plain = by_folder["peer-001", "Music\\Test Artist\\Test Album"]
    assert [f.extension for f in plain.files] == ["flac"] * 4
    assert plain.has_cue and plain.has_free_slot and plain.upload_speed == 900000
    f = plain.files[0]
    assert (f.bitrate, f.duration, f.bit_depth, f.sample_rate) == (None, 200, 16, 44100)
    assert f.path == "Music\\Test Artist\\Test Album\\01 Opening.flac"
    # peer-002: SK-F04, CD1 and Disc 2 fold into the album folder; locked files are ignored
    multi = by_folder["peer-002", "Music\\Test Artist\\Test Album (Deluxe)"]
    assert len(multi.files) == 4 and not multi.has_cue
    assert not multi.has_free_slot and multi.queue_length == 120
    # peer-003: two folders, two candidates
    assert {c.folder for c in cands if c.username == "peer-003"} == {
        "Music\\Test Artist\\Test Album (Single File)",
        "Music\\Test Artist\\Singles",
    }
    single = by_folder["peer-003", "Music\\Test Artist\\Test Album (Single File)"]
    assert len(single.files) == 1 and single.has_cue
    assert by_folder["peer-003", "Music\\Test Artist\\Singles"].files[0].bitrate == 320
    assert len(cands) == 4


async def test_search_polls_until_complete(make_client: Make) -> None:
    seen: list[httpx.Request] = []
    polls = iter(
        [load("search_running.json"), load("search_running.json"), load("search_complete.json")]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, json=load("search_running.json"))
        if request.url.path.endswith("/responses"):
            return httpx.Response(200, json=load("responses.json"))
        return httpx.Response(200, json=next(polls))

    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    src = SlskdSource(make_client("slskd", handler), BASE + "/", "key-123", sleep=sleep)
    cands = await src.search("Test Artist Test Album")
    assert len(cands) == 4 and sleeps == [1.0, 1.0]
    post = seen[0]
    assert post.method == "POST" and post.url.path == "/api/v0/searches"
    assert post.headers["X-API-Key"] == "key-123"
    body = json.loads(post.content)
    assert body["searchText"] == "Test Artist Test Album"
    assert seen[-1].url.path == f"/api/v0/searches/{body['id']}/responses"


async def test_search_gives_up(make_client: Make) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load("search_running.json"))

    src = SlskdSource(make_client("slskd", handler), BASE, "k", sleep=_no_sleep, max_polls=3)
    with pytest.raises(TransientError):
        await src.search("x")


@pytest.mark.parametrize("code", [401, 403])
async def test_sk_p06_auth_failure_names_the_api_key(make_client: Make, code: int) -> None:
    src = source(make_client, lambda r: httpx.Response(code))
    with pytest.raises(PermanentError, match="API_KEY"):
        await src.search("x")


async def test_sk_p06_server_error_is_transient(make_client: Make) -> None:
    src = source(make_client, lambda r: httpx.Response(500))
    with pytest.raises(TransientError):
        await src.search("x")


async def test_sk_p06_timeout_is_transient(make_client: Make) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(TransientError):
        await source(make_client, handler).search("x")


async def test_client_error_is_permanent(make_client: Make) -> None:
    with pytest.raises(PermanentError):
        await source(make_client, lambda r: httpx.Response(404)).search("x")


def candidate() -> Candidate:
    cands = map_responses(load("responses.json"))  # pyright: ignore[reportArgumentType]
    return cands[0]


async def test_enqueue_posts_files(make_client: Make) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"enqueued": [], "failed": []})

    await source(make_client, handler).enqueue(candidate())
    assert seen[0].url.path == "/api/v0/transfers/downloads/peer-001"
    body = json.loads(seen[0].content)
    assert [b["filename"] for b in body][0] == "Music\\Test Artist\\Test Album\\01 Opening.flac"
    assert len(body) == 4 and all(b["size"] > 0 for b in body)


async def test_enqueue_failure_is_permanent(make_client: Make) -> None:
    src = source(make_client, lambda r: httpx.Response(201, json={"enqueued": [], "failed": ["x"]}))
    with pytest.raises(PermanentError):
        await src.enqueue(candidate())


async def test_status_aggregates_transfers(make_client: Make) -> None:
    body = load("downloads_peer-001.json")
    src = source(make_client, lambda r: httpx.Response(200, json=body))
    info = await src.status(candidate())
    assert info.state is TransferStatus.IN_PROGRESS
    assert info.bytes == 880 * 125 * 200 + 1000


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (["Completed, Succeeded"] * 4, TransferStatus.DONE),
        (["Completed, Succeeded"] * 3 + ["Completed, Errored"], TransferStatus.FAILED),
        (["Completed, Succeeded"] * 3 + ["Queued, Remotely"], TransferStatus.QUEUED),
        (["Requested"] * 4, TransferStatus.QUEUED),
    ],
)
async def test_status_states(
    make_client: Make, states: list[str], expected: TransferStatus
) -> None:
    cand = candidate()
    files = [
        {"filename": f.path, "state": s, "bytesTransferred": 0}
        for f, s in zip(cand.files, states, strict=True)
    ]
    body = {"username": "peer-001", "directories": [{"directory": "d", "files": files}]}
    src = source(make_client, lambda r: httpx.Response(200, json=body))
    assert (await src.status(cand)).state is expected


async def test_status_before_all_files_are_listed(make_client: Make) -> None:
    cand = Candidate("peer-001", "d", (CandidateFile("d\\a.flac", 1, "flac"),))
    body = {"username": "peer-001", "directories": []}
    src = source(make_client, lambda r: httpx.Response(200, json=body))
    assert (await src.status(cand)).state is TransferStatus.QUEUED


def search_handler(responses: list[object]) -> Callable[[httpx.Request], httpx.Response]:
    it = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=load("search_running.json"))
        if request.url.path.endswith("/responses"):
            return httpx.Response(200, json=next(it, []))
        return httpx.Response(200, json=load("search_complete.json"))

    return handler


async def test_search_retries_responses_not_yet_persisted(make_client: Make) -> None:
    handler = search_handler([[], load("responses.json")])
    assert len(await source(make_client, handler).search("x")) == 4


async def test_search_persistently_empty_responses_is_transient(make_client: Make) -> None:
    with pytest.raises(TransientError):
        await source(make_client, search_handler([])).search("x")


async def test_search_with_no_results_returns_immediately(make_client: Make) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/responses"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={**load("search_complete.json"), "responseCount": 0})  # pyright: ignore[reportArgumentType]

    assert await source(make_client, handler).search("x") == []
