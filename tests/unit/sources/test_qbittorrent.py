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
            return httpx.Response(200, text=self.add)
        if path == "torrents/delete":
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
    with pytest.raises(AuthError, match="MIXSYNC_QBITTORRENT_USERNAME"):
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
