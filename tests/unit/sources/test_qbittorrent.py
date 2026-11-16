"""qBittorrent's Web API. The cassettes follow qBittorrent's official WebUI API (v2) docs; they
were written to that schema, not recorded from a live instance."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.config import Settings
from mixsync.core.errors import AuthError, PermanentError
from mixsync.core.matching import Candidate, CandidateFile, TransferStatus
from mixsync.ratelimit.client import PoliteClient
from mixsync.sources.qbittorrent import CATEGORY, Qbittorrent

CASSETTES = Path(__file__).parents[2] / "cassettes" / "qbittorrent"
BASE = "http://qbittorrent:8080"
SAVE = Path("/torrents")
HASH = "777fdacdb4a99c43a484126da5682b6b8a0c875c"
NAME = "Test Artist - Test Album (2020) [FLAC]"
CAND = Candidate(
    "ExampleTracker",
    NAME,
    (
        CandidateFile(f"{NAME}\\01 - Opening.flac", 30_000_000, "flac"),
        CandidateFile(f"{NAME}\\02 - Middle.flac", 31_000_000, "flac"),
    ),
    source="prowlarr",
    infohash=HASH,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class FakeQbit:
    """qBittorrent's Web API: a SID cookie from `auth/login`, 403 without it."""

    def __init__(self, **torrent: Any) -> None:
        self.torrents: list[dict[str, Any]] = [
            {**json.loads((CASSETTES / "info.json").read_text())[0], **torrent}
        ]
        self.seen: list[httpx.Request] = []
        self.login = "Ok."
        self.add = "Ok."
        self.stop: dict[str, Any] | None = {"state": "stoppedDL"}  # None: no torrents/stop (v4)
        self.on_add: dict[str, Any] | None = None  # the torrent an accepted add puts in the list
        self.files: list[dict[str, Any]] = json.loads((CASSETTES / "pack-files.json").read_text())
        self.prio_status = 200

    def files_reply(self, request: httpx.Request) -> httpx.Response:
        if not any(t["hash"] == request.url.params["hash"] for t in self.torrents):
            return httpx.Response(404, text="Torrent hash was not found")
        return httpx.Response(200, json=self.files)

    def prio_reply(self, request: httpx.Request) -> httpx.Response:
        if self.prio_status != 200:
            return httpx.Response(self.prio_status, text="Conflict")
        f = form(request)
        for i in f["id"][0].split("|"):
            self.files[int(i)]["priority"] = int(f["priority"][0])
        return httpx.Response(200)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        path = request.url.path.removeprefix("/api/v2/")
        if path == "auth/login":
            ok = self.login == "Ok."
            cookie = {"set-cookie": "SID=s3ss10n; HttpOnly; path=/"} if ok else {}
            return httpx.Response(200, text=self.login, headers=cookie)
        if "SID=s3ss10n" not in request.headers.get("cookie", ""):
            return httpx.Response(403, text="Forbidden")
        if path == "torrents/info":
            p = request.url.params
            rows = [
                t
                for t in self.torrents
                if t["hash"] in p["hashes"].split("|") and t["category"] == p["category"]
            ]
            return httpx.Response(200, json=rows)
        if path == "torrents/add":
            if self.add == "Ok." and self.on_add is not None:
                self.torrents.append(self.on_add)
            return httpx.Response(200, text=self.add)
        if path == "torrents/files":
            return self.files_reply(request)
        if path == "torrents/filePrio":
            return self.prio_reply(request)
        if path == "torrents/start" and self.stop is not None:
            return httpx.Response(200)
        if path == "torrents/resume" and self.stop is None:  # qBittorrent < 5
            return httpx.Response(200)
        if path == "torrents/delete":
            return httpx.Response(200)
        if path == "torrents/stop" and self.stop is not None:
            for t in self.torrents:
                t.update(self.stop)  # what the torrent looks like once stopped
            return httpx.Response(200)
        if path == "torrents/pause" and self.stop is None:  # qBittorrent < 5
            return httpx.Response(200)
        return httpx.Response(404)

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.seen if r.url.path == f"/api/v2/{path}"]


def qbit(settings: Settings, sessions: sessionmaker[Session], fake: FakeQbit) -> Qbittorrent:
    clock = FakeClock()
    client = PoliteClient(
        "qbittorrent",
        settings,
        sessions,
        clock=clock,
        transport=httpx.MockTransport(fake),
        sleep=clock.sleep,
    )
    return Qbittorrent(client, BASE, "admin", "adminadmin", SAVE)


def form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode())


Env = Callable[..., tuple[Qbittorrent, FakeQbit]]


@pytest.fixture
def make(settings: Settings, sessions: sessionmaker[Session]) -> Env:
    def build(**torrent: Any) -> tuple[Qbittorrent, FakeQbit]:
        fake = FakeQbit(**torrent)
        return qbit(settings, sessions, fake), fake

    return build


async def test_logs_in_on_a_403_and_keeps_the_session_cookie(make: Env) -> None:
    client, fake = make()
    assert await client.info(HASH) is not None
    assert await client.info(HASH) is not None
    (login,) = fake.calls("auth/login")
    assert form(login) == {"username": ["admin"], "password": ["adminadmin"]}
    assert [r.url.path for r in fake.seen].count("/api/v2/torrents/info") == 3  # 403, then 2 ok
    assert fake.calls("torrents/info")[-1].url.params["category"] == CATEGORY


async def test_a_rejected_login_is_an_auth_error(make: Env) -> None:
    client, fake = make()
    fake.login = "Fails."
    with pytest.raises(
        AuthError, match=r"_password on /settings \(or MIXSYNC_QBITTORRENT_USERNAME"
    ):
        await client.info(HASH)


async def test_add_puts_the_torrent_in_the_mixsync_category_under_its_save_path(
    make: Env,
) -> None:
    client, fake = make()
    await client.add(b"d4:infod4:name1:xee", HASH)
    (add,) = [r for r in fake.calls("torrents/add") if r.headers.get("cookie")]
    body = add.content
    assert b"d4:infod4:name1:xee" in body
    for field, value in (
        ("category", "mixsync"),
        ("savepath", "/torrents"),
        ("autoTMM", "false"),
        ("contentLayout", "Original"),
    ):
        assert f'name="{field}"\r\n\r\n{value}\r\n'.encode() in body


async def test_a_refused_add_is_permanent_unless_the_torrent_is_already_there(
    make: Env,
) -> None:
    client, fake = make()
    fake.add = "Fails."
    await client.add(b"torrent", HASH)  # a re-run: qBittorrent already has it
    fake.torrents = []
    with pytest.raises(PermanentError, match="refused the torrent"):
        await client.add(b"torrent", HASH)


async def test_status_is_done_only_at_100_percent(make: Env) -> None:
    client, fake = make()
    info = await client.status(CAND)
    assert (info.state, info.bytes, info.local_paths) == (
        TransferStatus.IN_PROGRESS,
        45_150_000,
        (),
    )
    fake.torrents[0] |= {"progress": 0, "completed": 0, "state": "queuedDL"}
    assert (await client.status(CAND)).state is TransferStatus.QUEUED
    fake.torrents[0] |= {"progress": 1, "completed": 90_152_048, "state": "uploading"}
    done = await client.status(CAND)
    assert done.state is TransferStatus.DONE
    assert done.local_paths == (
        f"/torrents/{NAME}/01 - Opening.flac",
        f"/torrents/{NAME}/02 - Middle.flac",
    )


async def test_an_errored_or_vanished_torrent_has_failed(make: Env) -> None:
    client, fake = make(state="error")
    assert (await client.status(CAND)).state is TransferStatus.FAILED
    fake.torrents[0]["category"] = "someone-elses"  # outside our category: not ours
    assert (await client.status(CAND)).state is TransferStatus.FAILED


async def test_only_an_unfinished_torrent_is_removed_with_its_files(make: Env) -> None:
    client, fake = make()
    await client.remove_unfinished(HASH)
    (delete,) = fake.calls("torrents/delete")
    assert form(delete) == {"hashes": [HASH], "deleteFiles": ["true"]}
    finished, fake = make(progress=1, state="stalledUP")
    await finished.remove_unfinished(HASH)
    assert fake.calls("torrents/delete") == []  # a seeding download: left to qBittorrent's rules


async def test_a_torrent_path_that_climbs_out_of_the_save_dir_is_refused(make: Env) -> None:
    client, fake = make(progress=1, state="uploading")
    for bad in (f"{NAME}\\..\\..\\etc\\passwd", f"{NAME}\\a/b.flac", f"{NAME}\\\\x.flac"):
        evil = Candidate("t", NAME, (CandidateFile(bad, 1, "flac"),), infohash=HASH)
        with pytest.raises(PermanentError, match="unsafe path"):
            await client.status(evil)


@pytest.mark.parametrize("state", ["checkingUP", "checkingResumeData", "stalledUP", "moving"])
async def test_a_rechecking_or_seeding_torrent_is_never_removed_whatever_its_progress(
    make: Env, state: str
) -> None:
    client, fake = make(progress=0.3, state=state)  # a recheck restarts progress from 0
    await client.remove_unfinished(HASH)
    assert fake.calls("torrents/delete") == [] and fake.calls("torrents/stop") == []


async def test_a_torrent_that_finishes_while_being_stopped_is_kept(make: Env) -> None:
    client, fake = make()
    fake.stop = {"progress": 1, "amount_left": 0, "state": "stoppedUP"}
    await client.remove_unfinished(HASH)
    assert len(fake.calls("torrents/stop")) == 1 and fake.calls("torrents/delete") == []


async def test_an_unfinished_torrent_is_paused_before_removal_on_qbittorrent_4(make: Env) -> None:
    client, fake = make()
    fake.stop = None
    await client.remove_unfinished(HASH)
    assert len(fake.calls("torrents/pause")) == 1 and len(fake.calls("torrents/delete")) == 1


# A pack (ADR 0026): tests/cassettes/prowlarr/pack.torrent, whose files /torrents/files lists in
# pack-files.json. The request wants the "2020 - Test Album" folder: indexes 2 and 3.
PACK = "Test Artist - Discography (2018-2022) [FLAC]"
PACK_HASH = "af7ca8794235068c99fa7e04ea4d7180b064ce37"
TEST_ALBUM = [f"{PACK}\\2020 - Test Album\\0{i}.flac" for i in ("1 - Opening", "2 - Middle")]
FIRST_ALBUM = [f"{PACK}\\2018 - First Album\\0{i}.flac" for i in ("1 - Alpha", "2 - Beta")]


def pack_row(**fields: Any) -> dict[str, Any]:
    row = json.loads((CASSETTES / "info.json").read_text())[0]
    return {**row, "hash": PACK_HASH, "name": PACK, "state": "stoppedDL", **fields}


def selected(fake: FakeQbit) -> list[int]:
    return [f["index"] for f in fake.files if f["priority"]]


async def test_a_pack_is_added_stopped_with_only_its_folder_selected_then_started(
    make: Env,
) -> None:
    client, fake = make()
    fake.on_add = pack_row()
    await client.add(b"pack", PACK_HASH, TEST_ALBUM)
    (add,) = [r for r in fake.calls("torrents/add") if r.headers.get("cookie")]
    for field in ("stopped", "paused"):  # qBittorrent 5.x reads one, 4.6 the other
        assert f'name="{field}"\r\n\r\ntrue\r\n'.encode() in add.content
    (prio,) = fake.calls("torrents/filePrio")
    assert form(prio) == {"hash": [PACK_HASH], "id": ["0|1|4|5|6"], "priority": ["0"]}
    assert selected(fake) == [2, 3]
    paths = [r.url.path.removeprefix("/api/v2/") for r in fake.seen if r.method == "POST"]
    assert paths[-3:] == ["torrents/add", "torrents/filePrio", "torrents/start"]


async def test_a_pack_is_resumed_on_qbittorrent_4(make: Env) -> None:
    client, fake = make()
    fake.on_add, fake.stop = pack_row(), None
    await client.add(b"pack", PACK_HASH, TEST_ALBUM)
    assert len(fake.calls("torrents/resume")) == 1 and selected(fake) == [2, 3]


async def test_a_pack_whose_selection_fails_is_deleted_and_never_started(make: Env) -> None:
    client, fake = make()
    fake.on_add, fake.prio_status = pack_row(), 409
    with pytest.raises(PermanentError, match="could not select the pack folder"):
        await client.add(b"pack", PACK_HASH, TEST_ALBUM)
    (delete,) = fake.calls("torrents/delete")
    assert form(delete) == {"hashes": [PACK_HASH], "deleteFiles": ["true"]}
    assert fake.calls("torrents/start") == []


async def test_a_second_folder_of_a_shared_pack_is_added_to_the_selection(make: Env) -> None:
    client, fake = make()
    fake.torrents = [pack_row(state="stalledUP", progress=1, amount_left=0)]
    for f in fake.files:  # the first request's folder, already downloaded
        f["priority"] = int(f["index"] in (2, 3))
    await client.add(b"pack", PACK_HASH, FIRST_ALBUM)
    assert [r for r in fake.calls("torrents/add") if r.headers.get("cookie")] == []
    (prio,) = fake.calls("torrents/filePrio")  # never a priority 0: nothing is deselected
    assert form(prio) == {"hash": [PACK_HASH], "id": ["0|1"], "priority": ["1"]}
    assert selected(fake) == [0, 1, 2, 3] and len(fake.calls("torrents/start")) == 1
    await client.add(b"pack", PACK_HASH, TEST_ALBUM)  # already selected: nothing to do
    assert len(fake.calls("torrents/filePrio")) == 1 and len(fake.calls("torrents/start")) == 1


async def test_a_folder_not_in_a_shared_pack_is_refused_without_touching_it(make: Env) -> None:
    client, fake = make()
    fake.torrents = [pack_row(state="downloading")]
    with pytest.raises(PermanentError, match="not in the torrent"):
        await client.add(b"pack", PACK_HASH, [f"{PACK}\\9999 - Nope\\01 - x.flac"])
    assert fake.calls("torrents/filePrio") == [] and fake.calls("torrents/delete") == []


async def test_deselect_drops_only_the_given_files_of_an_unfinished_pack(make: Env) -> None:
    client, fake = make()
    fake.torrents = [pack_row(state="downloading")]
    for f in fake.files:
        f["priority"] = int(f["index"] in (0, 1, 2, 3))
    await client.deselect(PACK_HASH, FIRST_ALBUM)  # the caller left out what others need
    (prio,) = fake.calls("torrents/filePrio")
    assert form(prio) == {"hash": [PACK_HASH], "id": ["0|1"], "priority": ["0"]}
    assert selected(fake) == [2, 3]


@pytest.mark.parametrize("finished", [True, False])
async def test_deselect_never_touches_a_finished_or_whole_torrent(
    make: Env, finished: bool
) -> None:
    client, fake = make()
    if finished:
        fake.torrents = [pack_row(state="stalledUP", progress=1, amount_left=0)]
        fake.files[0]["priority"] = 0
    else:  # every file wanted: added whole, not a pack selection
        fake.torrents = [pack_row(state="downloading")]
    await client.deselect(PACK_HASH, TEST_ALBUM)
    assert fake.calls("torrents/filePrio") == []
