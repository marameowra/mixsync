"""Usenet through Prowlarr and SABnzbd (#327). A request Soulseek cannot fill is fetched as an
NZB and goes through the same verify pipeline. Usenet downloads do not seed: after a clean
import their files are released (journaled), like Soulseek ones. A failed job drops the
candidate and is cleared from SABnzbd. Prowlarr, the indexer and SABnzbd are httpx fakes."""

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
from sqlalchemy import select

from mixsync.db.journal import SqlJournal
from mixsync.db.models.safety import FileOp
from mixsync.library.fileops import FileOps
from mixsync.library.importer import Importer
from mixsync.ratelimit.client import PoliteClient
from mixsync.sources.prowlarr import ProwlarrSource
from mixsync.sources.sabnzbd import Sabnzbd

from .conftest import Env
from .fakes import FakeSource
from .test_pipeline import FIRST, SECOND, library, request, status, tried

NAME = "Alpha Band - Alpha Album (2020) [MP3]"
PROWLARR = "http://prowlarr:9696"
INDEXER = "https://indexer.example/getnzb/1.nzb?r=INDEXER-KEY"
KEY, SAB_KEY = "prowlarr-key", "sab-key"


class FakeClock:
    def __init__(self) -> None:
        self.t = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def form(r: httpx.Request) -> dict[str, str]:
    if "multipart" in r.headers["content-type"]:
        fields = re.findall(rb'name="(\w+)"\r\n\r\n([^\r]*)\r\n', r.content)
        return {k.decode(): v.decode() for k, v in fields}
    return {k: v[0] for k, v in parse_qs(r.content.decode()).items()}


class Usenet:
    """Prowlarr's search and download link (a 301 to the indexer, as Prowlarr always answers
    for Usenet), the indexer's NZB of the Alpha Album's tagged files, and SABnzbd's API.
    `addfile` writes the files under the job's folder in the SABnzbd dir, as SABnzbd would."""

    def __init__(self, tmp: Path) -> None:
        maker = FakeSource(tmp / "unused", tmp / "nzb-src")
        maker.add("x", NAME)  # the usual tagged files, no MBIDs
        self.blobs = maker.blobs[("x", NAME)]
        files = "".join(
            f'<file subject="&quot;{n}&quot; yEnc (1/1)"><segments>'
            # yEnc-encoded sizes, as a real NZB lists them: a bit above the decoded file's
            f'<segment bytes="{len(b) * 102 // 100 + 500}" number="1">{i}@x</segment>'
            "</segments></file>"
            for i, (n, b) in enumerate(sorted(self.blobs.items()))
        )
        self.nzb = f'<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">{files}</nzb>'.encode()
        self.dir = tmp / "usenet"
        self.status = "Completed"
        self.share = 1.0  # of each decoded file SABnzbd writes
        self.job: dict[str, Any] | None = None
        self.deleted: list[dict[str, str]] = []

    def prowlarr(self, r: httpx.Request) -> httpx.Response:
        if r.url.host == "indexer.example":
            assert "X-Api-Key" not in r.headers
            return httpx.Response(200, content=self.nzb)
        if r.url.path == "/api/v1/search":
            release = {  # null fields (seeders, magnetUrl, infoHash) are omitted
                "title": NAME,
                "size": sum(map(len, self.blobs.values())),
                "indexer": "ExampleUsenet",
                "protocol": "usenet",
                "downloadUrl": f"{PROWLARR}/2/download?apikey={KEY}&link=a&file=1",
            }
            return httpx.Response(200, json=[release])
        assert r.headers["X-Api-Key"] == KEY and r.url.params["apikey"] == KEY
        return httpx.Response(301, headers={"Location": INDEXER})

    def sab(self, r: httpx.Request) -> httpx.Response:
        f = form(r)
        assert f["apikey"] == SAB_KEY and SAB_KEY not in str(r.url)
        if f["mode"] == "addfile":
            assert self.nzb in r.content and b"INDEXER-KEY" not in r.content
            folder = self.dir / f["nzbname"]
            for name, blob in self.blobs.items():
                folder.mkdir(parents=True, exist_ok=True)
                (folder / name).write_bytes(blob[: int(len(blob) * self.share)])
            self.job = {"nzo_id": "SABnzbd_nzo_1", "name": f["nzbname"], "category": f["cat"]}
            self.job |= {"storage": str(folder), "bytes": 1}
            return httpx.Response(200, json={"status": True, "nzo_ids": ["SABnzbd_nzo_1"]})
        if f.get("name") == "delete":
            self.deleted.append({k: v for k, v in f.items() if k not in ("apikey", "output")})
            self.job = None
            return httpx.Response(200, json={"status": True, "nzo_ids": [f["value"]]})
        slots = [{**self.job, "status": self.status}] if self.job else []
        if f["mode"] == "queue":
            return httpx.Response(200, json={"queue": {"slots": []}})
        return httpx.Response(200, json={"history": {"slots": slots}})


def wire(env: Env, tmp_path: Path) -> Usenet:
    """Soulseek (the FakeSource, empty) first, then Prowlarr + SABnzbd, as the worker wires
    them; FileOps knows the SABnzbd dir."""
    u = Usenet(tmp_path)
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

    sab = Sabnzbd(client("sabnzbd", u.sab), "http://sabnzbd:8080", SAB_KEY, u.dir)
    src = ProwlarrSource(
        client("prowlarr", u.prowlarr), PROWLARR, KEY, None, frozenset({"usenet"}), sab
    )
    env.ops = FileOps(
        env.data_dir,
        env.downloads,
        SqlJournal(env.sessions, env.clock),
        env.clock,
        usenet_dir=u.dir,
    )
    importer = Importer(env.ops, env.sessions, env.clock, env.settings.path_template)
    env.pipeline = replace(
        env.pipeline, sources={"slskd": env.source, "prowlarr": src}, importer=importer
    )
    env.handlers = env.pipeline.handlers()
    return u


async def test_a_request_soulseek_cannot_fill_is_fetched_from_usenet_and_released(
    env: Env, tmp_path: Path
) -> None:
    u = wire(env, tmp_path)
    rid = request(env)
    await env.drain()
    assert env.source.queries and env.source.enqueued == []  # Soulseek was asked first
    assert status(env, rid) == "imported" and library(env) == [FIRST, SECOND]
    with env.sessions() as s:
        released = s.scalars(
            select(FileOp.src).where(FileOp.kind == "release", FileOp.status == "done")
        ).all()
    assert {p.rsplit("/", 1)[1] for p in released if p.startswith(str(u.dir))} == set(u.blobs)
    assert not list(u.dir.rglob("*.mp3"))  # the downloads are gone, journaled
    # the finished job leaves SABnzbd's history; its folder is never SABnzbd's to delete
    assert u.deleted == [
        {"mode": "history", "name": "delete", "value": "SABnzbd_nzo_1", "archive": "0"}
    ]


async def test_a_failed_job_drops_the_candidate_and_is_cleared_from_sabnzbd(
    env: Env, tmp_path: Path
) -> None:
    u = wire(env, tmp_path)
    u.status = "Failed"
    rid = request(env)
    await env.drain(until=lambda: status(env, rid) == "no_results")
    assert library(env) == [] and tried(env, rid) == [["ExampleUsenet", NAME, "prowlarr"]]
    assert u.deleted == [
        {
            "mode": "history",
            "name": "delete",
            "value": "SABnzbd_nzo_1",
            "archive": "0",
            "del_files": "1",
        }
    ]


async def test_a_completed_job_with_files_well_short_of_the_nzb_is_dropped(
    env: Env, tmp_path: Path
) -> None:
    """The NZB's encoded sizes allow a decoded file down to 90 %; far less is a broken file."""
    u = wire(env, tmp_path)
    u.share = 0.5
    rid = request(env)
    await env.drain(until=lambda: status(env, rid) == "no_results")
    assert library(env) == [] and tried(env, rid) == [["ExampleUsenet", NAME, "prowlarr"]]
