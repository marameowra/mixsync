import json
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from mixsync.core.errors import PermanentError, TransientError
from mixsync.core.matching import Candidate, CandidateFile, TransferStatus
from mixsync.ratelimit.client import PoliteClient
from mixsync.sources.slskd import SlskdSource, map_responses

FX = Path(__file__).parents[2] / "fixtures" / "slskd"
BASE = "http://slskd:5030"
DOWNLOADS = Path("/downloads")

Make = Callable[[str, Callable[[httpx.Request], httpx.Response]], PoliteClient]


def load(name: str) -> object:
    return json.loads((FX / name).read_text())


async def _no_sleep(_s: float) -> None:
    return None


def source(make_client: Make, handler: Callable[[httpx.Request], httpx.Response]) -> SlskdSource:
    return SlskdSource(
        make_client("slskd", handler), BASE, "key-123", downloads_dir=DOWNLOADS, sleep=_no_sleep
    )


def test_only_formats_import_takes_become_candidates() -> None:
    names = ["a.opus", "b.aif", "c.wav", "d.wma", "e.aac", "f.ape"]
    files = [{"filename": f"Music\\A\\B\\{n}", "size": 1, "isLocked": False} for n in names]
    resp = [{"username": "p", "hasFreeUploadSlot": True, "queueLength": 0, "uploadSpeed": 1,
             "files": files}]  # fmt: skip
    (cand,) = map_responses(resp)
    assert sorted(f.extension for f in cand.files) == ["aif", "opus", "wav"]


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

    src = SlskdSource(
        make_client("slskd", handler), BASE + "/", "key-123", downloads_dir=DOWNLOADS, sleep=sleep
    )
    cands = await src.search("Test Artist Test Album")
    assert len(cands) == 4 and sleeps == [1.0, 1.0]
    post = seen[0]
    assert post.method == "POST" and post.url.path == "/api/v0/searches"
    assert post.headers["X-API-Key"] == "key-123"
    body = json.loads(post.content)
    assert body["searchText"] == "Test Artist Test Album"
    assert {
        "responseLimit",
        "fileLimit",
        "searchTimeout",
        "minimumPeerUploadSpeed",
        "maximumPeerQueueLength",
    } <= body.keys()
    assert body["responseLimit"] > 100
    assert seen[-1].url.path == f"/api/v0/searches/{body['id']}/responses"


async def test_search_gives_up_and_deletes_the_search(make_client: Make) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json=load("search_running.json"))

    src = SlskdSource(
        make_client("slskd", handler),
        BASE,
        "k",
        downloads_dir=DOWNLOADS,
        sleep=_no_sleep,
        max_polls=3,
    )
    with pytest.raises(TransientError):
        await src.search("x")
    post, last = seen[0], seen[-1]
    search_id = json.loads(post.content)["id"]
    assert last.method == "DELETE" and last.url.path == f"/api/v0/searches/{search_id}"


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


async def test_clear_cancels_and_removes_only_this_candidates_transfers(make_client: Make) -> None:
    cand = Candidate("p", "d", (CandidateFile("d\\a.flac", 1, "flac"),))
    files = [{"id": "ID-A", "filename": "d\\a.flac"}, {"id": "ID-B", "filename": "d\\other.flac"}]
    body = {"username": "p", "directories": [{"directory": "d", "files": files}]}
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body) if request.method == "GET" else httpx.Response(204)

    await source(make_client, handler).clear(cand)
    (delete,) = [r for r in seen if r.method == "DELETE"]
    assert delete.url.path == "/api/v0/transfers/downloads/p/ID-A"
    assert delete.url.params["remove"] == "true"


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


@pytest.mark.parametrize(
    ("failure", "retry"),
    [
        ("Completed, Errored", True),
        ("Completed, TimedOut", True),
        ("Completed, Rejected", False),
        ("Completed, Cancelled", False),
    ],
)
async def test_only_timed_out_or_errored_files_are_worth_a_retry(
    make_client: Make, failure: str, retry: bool
) -> None:
    cand = candidate()
    states = ["Completed, Succeeded"] * 3 + [failure]
    files = [
        {"filename": f.path, "state": s, "bytesTransferred": 0}
        for f, s in zip(cand.files, states, strict=True)
    ]
    body = {"username": "peer-001", "directories": [{"directory": "d", "files": files}]}
    info = await source(make_client, lambda r: httpx.Response(200, json=body)).status(cand)
    assert info.state is TransferStatus.FAILED
    assert info.retry == ((cand.files[3],) if retry else ())


async def test_a_requeued_file_is_judged_by_its_newest_transfer(make_client: Make) -> None:
    cand = candidate()
    files = [
        {"filename": f.path, "state": "Completed, Succeeded", "bytesTransferred": 0}
        for f in cand.files
    ]
    old = {**files[3], "state": "Completed, Errored", "requestedAt": "2026-01-01T00:00:00Z"}
    new = {**files[3], "state": "Queued, Remotely", "requestedAt": "2026-01-01T00:05:00Z"}
    body = {
        "username": "peer-001",
        "directories": [{"directory": "d", "files": [*files[:3], old, new]}],
    }
    info = await source(make_client, lambda r: httpx.Response(200, json=body)).status(cand)
    assert info.state is TransferStatus.QUEUED


async def test_a_retry_enqueues_only_the_failed_files(make_client: Make) -> None:
    cand = candidate()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"enqueued": [], "failed": []})

    await source(make_client, handler).enqueue(replace(cand, files=cand.files[3:]))
    (post,) = seen
    assert post.url.path == "/api/v0/transfers/downloads/peer-001"
    assert json.loads(post.content) == [
        {"filename": cand.files[3].path, "size": cand.files[3].size}
    ]


async def test_done_reports_local_paths_by_each_files_own_folder(make_client: Make) -> None:
    cand = Candidate(
        "p",
        "Music\\A\\Album",
        (
            CandidateFile("Music\\A\\Album\\01 x.flac", 1, "flac"),
            CandidateFile("Music\\A\\Album\\CD2\\01 y.flac", 1, "flac"),
        ),
    )
    files = [{"filename": f.path, "state": "Completed, Succeeded", "bytesTransferred": 1}
             for f in cand.files]  # fmt: skip
    body = {"username": "p", "directories": [{"directory": "d", "files": files}]}
    info = await source(make_client, lambda r: httpx.Response(200, json=body)).status(cand)
    assert info.local_paths == ("/downloads/Album/01 x.flac", "/downloads/CD2/01 y.flac")


@pytest.mark.parametrize(
    "bad", ["x\\/data/library/A/B\\01.flac", "x\\..\\01.flac", "x\\A\\\\01.flac", "x\\.\\01.flac"]
)
def test_unsafe_peer_paths_are_never_candidates(bad: str) -> None:
    good = "x\\Album\\01.flac"
    files = [{"filename": n, "size": 1} for n in (bad, good)]
    resp = {"username": "p", "hasFreeUploadSlot": True, "queueLength": 0, "uploadSpeed": 1}
    cands = map_responses([{**resp, "files": files}])
    assert [f.path for c in cands for f in c.files] == [good]


async def test_done_rejects_stored_candidate_with_unsafe_path(make_client: Make) -> None:
    bad = "x\\/etc\\01.flac"
    cand = Candidate("p", "x", (CandidateFile(bad, 1, "flac"),))
    files = [{"filename": bad, "state": "Completed, Succeeded", "bytesTransferred": 1}]
    body = {"username": "p", "directories": [{"directory": "d", "files": files}]}
    with pytest.raises(PermanentError):
        await source(make_client, lambda r: httpx.Response(200, json=body)).status(cand)


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


async def test_active_downloads_counts_only_transfers_moving_data(make_client: Make) -> None:
    body: Any = load("downloads_peer-001.json")
    for n, f in enumerate(body["directories"][0]["files"]):
        f["id"] = f"id-{n}"  # the fixture reuses one id; slskd's are distinct
    src = source(make_client, lambda r: httpx.Response(200, json=[body]))
    assert await src.active_downloads() == {"id-1": 1000}  # InProgress only; no queued/done


async def done_path(make_client: Make, root: Path) -> str:
    cand = Candidate("p", "Album", (CandidateFile("x\\Album\\01.flac", 4, "flac"),))
    done = {"state": "Completed, Succeeded", "bytesTransferred": 4}
    files = [{"filename": "x\\Album\\01.flac", **done}]
    body = {"username": "p", "directories": [{"directory": "d", "files": files}]}
    src = SlskdSource(
        make_client("slskd", lambda r: httpx.Response(200, json=body)),
        BASE,
        "key-123",
        downloads_dir=root,
        sleep=_no_sleep,
    )
    (path,) = (await src.status(cand)).local_paths
    return path


def put(root: Path, name: str, data: bytes, mtime: int) -> Path:
    p = root / "Album" / name
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(data)
    os.utime(p, (mtime, mtime))
    return p


async def test_done_uses_deduplicated_name_beside_a_leftover(
    make_client: Make, tmp_path: Path
) -> None:
    old = put(tmp_path, "01.flac", b"old", 1)  # rejected earlier download
    new = put(tmp_path, "01_638000000000000001.flac", b"new!", 3)
    put(tmp_path, "01_notticks.flac", b"xxxx", 9)
    assert await done_path(make_client, tmp_path) == str(new)
    assert old.read_bytes() == b"old"


async def test_done_prefers_newer_renamed_file_over_same_size_leftover(
    make_client: Make, tmp_path: Path
) -> None:
    put(tmp_path, "01.flac", b"aaaa", 1)
    new = put(tmp_path, "01_638000000000000001.flac", b"bbbb", 2)
    assert await done_path(make_client, tmp_path) == str(new)


async def test_done_finds_renamed_file_when_original_is_gone(
    make_client: Make, tmp_path: Path
) -> None:
    new = put(tmp_path, "01_638000000000000001.flac", b"bbbb", 2)
    assert await done_path(make_client, tmp_path) == str(new)


async def test_done_keeps_original_when_it_is_the_only_match(
    make_client: Make, tmp_path: Path
) -> None:
    orig = put(tmp_path, "01.flac", b"aaaa", 1)
    assert await done_path(make_client, tmp_path) == str(orig)


async def test_done_picks_newest_of_several_renamed_files(
    make_client: Make, tmp_path: Path
) -> None:
    put(tmp_path, "01_638000000000000001.flac", b"aaaa", 1)
    new = put(tmp_path, "01_638000000000000002.flac", b"bbbb", 5)
    put(tmp_path, "01_638000000000000003.flac", b"cccc", 2)
    assert await done_path(make_client, tmp_path) == str(new)
