"""Torrents through Prowlarr and qBittorrent (ADR 0022). A request Soulseek cannot fill is
fetched as a torrent and goes through the same verify and review pipeline. The torrent's files
are seeding downloads: copied into the library or quarantine, never released, moved or deleted,
on accept, reject, review or repick. A torrent dropped before it completes is removed with its
partial files. Prowlarr and qBittorrent are httpx fakes; nothing goes to the network."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mixsync.acquire.evidence import Evidence
from mixsync.db.journal import SqlJournal
from mixsync.db.models.safety import FileOp
from mixsync.library.fileops import FileOps
from mixsync.library.importer import Importer
from mixsync.ratelimit.client import PoliteClient
from mixsync.sources.prowlarr import ProwlarrSource
from mixsync.sources.qbittorrent import Qbittorrent

from .conftest import Env, login
from .fakes import ALBUM_B, FakeSource
from .test_pipeline import (
    FIRST,
    SECOND,
    decision_rows,
    library,
    quarantined,
    request,
    status,
    tried,
    veto_second_file,
)

TNAME = "Alpha Band - Alpha Album (2020) [MP3]"
PROWLARR = "http://prowlarr:9696"
KEY = "prowlarr-key"


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


class FakeClock:
    def __init__(self) -> None:
        self.t = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class Torrents:
    """Prowlarr's search and download link, and qBittorrent's Web API, over one torrent of the
    Alpha Album's tagged files. `add` writes the files under the save path, as qBittorrent
    would; `delete` removes them."""

    def __init__(self, tmp: Path) -> None:
        maker = FakeSource(tmp / "unused", tmp / "torrent-src")
        maker.add("x", TNAME)  # the usual tagged files, no MBIDs
        self.blobs = maker.blobs[("x", TNAME)]
        files = [{"length": len(b), "path": [n]} for n, b in sorted(self.blobs.items())]
        info = {"name": TNAME, "piece length": 16384, "pieces": b"", "files": files}
        self.torrent = bencode({"announce": "http://tracker.example/announce", "info": info})
        self.hash = hashlib.sha1(bencode(info), usedforsecurity=False).hexdigest()
        self.save = tmp / "torrents"
        self.progress, self.state, self.completed = 1.0, "uploading", 1
        self.added = False
        self.deleted: list[dict[str, list[str]]] = []

    def prowlarr(self, r: httpx.Request) -> httpx.Response:
        if r.url.path == "/api/v1/search":
            release = {
                "title": TNAME,
                "size": sum(map(len, self.blobs.values())),
                "indexer": "ExampleTracker",
                "protocol": "torrent",
                "downloadUrl": f"{PROWLARR}/1/download?apikey={KEY}&link=a&file=1",
                "seeders": 10,
            }
            return httpx.Response(200, json=[release])
        assert r.headers["X-Api-Key"] == KEY and r.url.params["apikey"] == KEY
        return httpx.Response(200, content=self.torrent)

    def qbit(self, r: httpx.Request) -> httpx.Response:
        path = r.url.path.removeprefix("/api/v2/")
        if path == "torrents/add":
            assert self.torrent in r.content
            for name, blob in self.blobs.items():
                f = self.save / TNAME / name
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(blob if self.progress == 1 else blob[: len(blob) // 2])
            self.added = True
            return httpx.Response(200, text="Ok.")
        if path == "torrents/info":
            row = {"hash": self.hash, "category": "mixsync", "progress": self.progress}
            row |= {"state": self.state, "completed": self.completed}
            return httpx.Response(200, json=[row] if self.added else [])
        if path == "torrents/delete":
            self.deleted.append(parse_qs(r.content.decode()))
            if parse_qs(r.content.decode())["deleteFiles"] == ["true"]:  # gone from the save path
                (self.save / TNAME).rename(self.save.parent / "qbittorrent-deleted")
            self.added = False
            return httpx.Response(200)
        return httpx.Response(404)

    def files(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in (self.save / TNAME).iterdir()}


def wire(env: Env, tmp_path: Path) -> Torrents:
    """Soulseek (the FakeSource, empty) first, then Prowlarr + qBittorrent, as the worker wires
    them; FileOps knows the qBittorrent save path."""
    t = Torrents(tmp_path)
    clock = FakeClock()

    def client(service: str, handler: Any) -> PoliteClient:
        return PoliteClient(
            service,
            env.settings,
            env.sessions,
            clock=clock,
            transport=httpx.MockTransport(handler),
            sleep=clock.sleep,
        )

    qbit = Qbittorrent(client("qbittorrent", t.qbit), "http://qbittorrent:8080", "", "", t.save)
    src = ProwlarrSource(
        client("prowlarr", t.prowlarr), PROWLARR, KEY, qbit, frozenset({"torrent"})
    )
    env.ops = FileOps(
        env.data_dir,
        env.downloads,
        SqlJournal(env.sessions, env.clock),
        env.clock,
        seeding_dir=t.save,
    )
    importer = Importer(env.ops, env.sessions, env.clock, env.settings.path_template)
    env.pipeline = replace(
        env.pipeline, sources={"slskd": env.source, "prowlarr": src}, importer=importer
    )
    env.handlers = env.pipeline.handlers()
    return t


def assert_seeding(env: Env, t: Torrents) -> None:
    """The torrent is still in qBittorrent with every file as downloaded, and nothing ever
    asked to release one of them."""
    assert t.added and t.deleted == [] and t.files() == t.blobs
    with env.sessions() as s:
        released = s.scalars(select(FileOp.src).where(FileOp.kind == "release")).all()
    assert not [r for r in released if r.startswith(str(t.save))]


async def test_a_request_soulseek_cannot_fill_is_fetched_as_a_torrent_and_imported(
    env: Env, tmp_path: Path
) -> None:
    t = wire(env, tmp_path)
    rid = request(env)
    await env.drain()
    assert env.source.queries and env.source.enqueued == []  # Soulseek was asked first
    assert status(env, rid) == "imported" and library(env) == [FIRST, SECOND]
    with env.sessions() as s:
        copied = s.scalars(select(FileOp.src).where(FileOp.kind == "copy")).all()
    want = {str(t.save / TNAME / n) for n in t.blobs}
    assert want <= set(copied)  # the imports copied from the save path (the cover is its own)
    assert_seeding(env, t)


async def test_a_reviewed_torrent_is_copied_to_quarantine_and_accepting_it_leaves_the_seed(
    env: Env, web: TestClient, tmp_path: Path
) -> None:
    t = wire(env, tmp_path)
    veto_second_file(env, TNAME)
    rid = request(env)
    await env.drain()
    assert status(env, rid) == "review" and len(quarantined(env, "review")) == 2
    (d,) = decision_rows(env, 2)
    paths = [f["local_path"] for f in Evidence.parse(d.evidence).files]
    assert paths and not [p for p in paths if p.startswith(str(t.save))]  # the copies
    assert_seeding(env, t)

    tok = login(web, "alice")
    assert web.post(f"/review/{d.id}/accept", data={"csrf_token": tok}).status_code == 303
    await env.drain()
    assert status(env, rid) == "imported" and library(env) == [FIRST]
    assert_seeding(env, t)


async def test_rejecting_a_reviewed_torrent_leaves_it_seeding(
    env: Env, web: TestClient, tmp_path: Path
) -> None:
    t = wire(env, tmp_path)
    veto_second_file(env, TNAME)
    rid = request(env)
    await env.drain()
    (d,) = decision_rows(env, 2)
    tok = login(web, "alice")
    assert web.post(f"/review/{d.id}/reject", data={"csrf_token": tok}).status_code == 303
    await env.drain(until=lambda: status(env, rid) == "no_results")
    assert tried(env, rid) == [["ExampleTracker", TNAME, "prowlarr"]]
    assert quarantined(env, "review") == [] and len(quarantined(env, "rejected")) == 2
    assert_seeding(env, t)


async def test_a_repick_verifies_the_quarantine_copies_and_leaves_the_seed(
    env: Env, web: TestClient, tmp_path: Path
) -> None:
    t = wire(env, tmp_path)
    veto_second_file(env, TNAME)
    rid = request(env)
    await env.drain()
    (d,) = decision_rows(env, 2)
    tok = login(web, "alice")
    data = {"csrf_token": tok, "release": str(ALBUM_B.release_id)}
    assert web.post(f"/review/{d.id}/repick", data=data).status_code == 303
    await env.drain()
    _, new = decision_rows(env, 2)
    assert new.release_mbid == ALBUM_B.release_id and status(env, rid) == "review"
    paths = [f["local_path"] for f in Evidence.parse(new.evidence).files]
    assert paths and not [p for p in paths if p.startswith(str(t.save))]
    assert_seeding(env, t)


@pytest.mark.parametrize("state", ["error", "stalledDL"])
async def test_a_torrent_dropped_before_it_completes_is_removed_with_its_files(
    env: Env, tmp_path: Path, state: str
) -> None:
    t = wire(env, tmp_path)
    t.progress, t.state, t.completed = 0.4, state, 1000  # failed, or no new bytes for 2 h
    rid = request(env)
    await env.drain(until=lambda: bool(t.deleted), cap=400)
    assert t.deleted == [{"hashes": [t.hash], "deleteFiles": ["true"]}]
    assert not (t.save / TNAME).exists() and library(env) == []
    await env.drain(until=lambda: status(env, rid) == "no_results")
    assert tried(env, rid) == [["ExampleTracker", TNAME, "prowlarr"]]
