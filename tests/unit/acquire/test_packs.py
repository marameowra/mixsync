"""Pack folders (ADR 0026): with torrent_pack_folders on, a request takes one album folder of a
multi-album torrent. qBittorrent downloads only that folder's files; a dropped folder is
skipped on the retry while the pack's next-best folder stays eligible. Prowlarr and qBittorrent
are httpx fakes; nothing goes to the network."""

import hashlib
from dataclasses import replace
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
from mixsync.sources.qbittorrent import Qbittorrent

from .conftest import Env
from .fakes import FakeSource
from .test_pipeline import FIRST, SECOND, library, request, status, tried
from .test_seeding import KEY, PROWLARR, FakeClock, bencode

PACK = "Alpha Band - Discography [MP3]"
FOLDERS = ("2020 - Alpha Album", "2021 - Alpha Album (Reissue)")  # equal matches: first wins


class Pack:
    """Prowlarr's search and download link over one pack holding the Alpha Album twice, and
    qBittorrent's Web API: `torrents/start` writes the selected files under the save path. The
    first add errors (a dead folder); after its delete, the next add downloads."""

    def __init__(self, tmp: Path) -> None:
        maker = FakeSource(tmp / "unused", tmp / "torrent-src")
        maker.add("x", "f")  # the usual tagged files, no MBIDs
        blobs = maker.blobs[("x", "f")]
        self.files = [(f"{d}/{n}", b) for d in FOLDERS for n, b in sorted(blobs.items())]
        listed = [{"length": len(b), "path": p.split("/")} for p, b in self.files]
        info = {"name": PACK, "piece length": 16384, "pieces": b"", "files": listed}
        self.torrent = bencode({"announce": "http://tracker.example/announce", "info": info})
        self.hash = hashlib.sha1(bencode(info), usedforsecurity=False).hexdigest()
        self.save = tmp / "torrents"
        self.added: dict[str, list[str]] | None = None
        self.priority: list[int] = []
        self.started = False
        self.adds = 0
        self.prios: list[dict[str, list[str]]] = []
        self.deleted: list[dict[str, list[str]]] = []

    def prowlarr(self, r: httpx.Request) -> httpx.Response:
        if r.url.path == "/api/v1/search":
            release = {
                "title": PACK,
                "size": sum(len(b) for _, b in self.files),
                "indexer": "ExampleTracker",
                "protocol": "torrent",
                "downloadUrl": f"{PROWLARR}/1/download?apikey={KEY}&link=a&file=1",
                "seeders": 10,
            }
            return httpx.Response(200, json=[release])
        return httpx.Response(200, content=self.torrent)

    def qbit(self, r: httpx.Request) -> httpx.Response:
        path = r.url.path.removeprefix("/api/v2/")
        if path == "torrents/add":
            assert self.torrent in r.content and b'name="stopped"\r\n\r\ntrue' in r.content
            self.adds += 1
            self.added, self.priority, self.started = {}, [1] * len(self.files), False
            return httpx.Response(200, text="Ok.")
        if path == "torrents/info":
            if self.added is None:
                return httpx.Response(200, json=[])
            done = self.started and self.adds > 1
            state = "uploading" if done else "error" if self.started else "stoppedDL"
            row = {"hash": self.hash, "category": "mixsync", "state": state, "completed": 1}
            row |= {"progress": 1 if done else 0.1, "amount_left": 0 if done else 1000}
            return httpx.Response(200, json=[row])
        if path == "torrents/files":
            rows = [
                {"index": i, "name": f"{PACK}/{p}", "priority": self.priority[i]}
                for i, (p, _) in enumerate(self.files)
            ]
            return httpx.Response(200, json=rows)
        if path == "torrents/filePrio":
            f = parse_qs(r.content.decode())
            self.prios.append(f)
            for i in f["id"][0].split("|"):
                self.priority[int(i)] = int(f["priority"][0])
            return httpx.Response(200)
        if path == "torrents/start":
            self.started = True
            for i, (p, blob) in enumerate(self.files):
                if self.priority[i]:
                    dest = self.save / PACK / p
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(blob)
            return httpx.Response(200)
        if path == "torrents/stop":
            return httpx.Response(200)
        if path == "torrents/delete":
            self.deleted.append(parse_qs(r.content.decode()))
            (self.save / PACK).rename(self.save.parent / "qbittorrent-deleted")
            self.added = None
            return httpx.Response(200)
        return httpx.Response(404)

    def on_disk(self) -> list[str]:
        return sorted(str(p.relative_to(self.save)) for p in self.save.rglob("*") if p.is_file())


def wire(env: Env, tmp_path: Path) -> Pack:
    t = Pack(tmp_path)
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
        client("prowlarr", t.prowlarr), PROWLARR, KEY, qbit, frozenset({"torrent"}), packs=True
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


async def test_a_dropped_pack_folder_is_skipped_and_the_next_best_folder_is_imported(
    env: Env, tmp_path: Path
) -> None:
    t = wire(env, tmp_path)
    rid = request(env)
    await env.drain(until=lambda: status(env, rid) == "imported", cap=400)
    assert tried(env, rid) == [["ExampleTracker", f"{PACK}\\{FOLDERS[0]}", "prowlarr"]]
    # the dead first folder: removed (no other request shares it), then added again for the next
    assert t.deleted == [{"hashes": [t.hash], "deleteFiles": ["true"]}] and t.adds == 2
    # the second add selected only the second folder: the first one's files set to 0
    assert t.prios[-1] == {"hash": [t.hash], "id": ["0|1"], "priority": ["0"]}
    names = ("01 Song One.mp3", "02 Song Two.mp3")
    assert t.on_disk() == [f"{PACK}/{FOLDERS[1]}/{n}" for n in names]
    assert library(env) == [FIRST, SECOND]
    with env.sessions() as s:
        copied = set(s.scalars(select(FileOp.src).where(FileOp.kind == "copy")).all())
    assert {str(t.save / PACK / FOLDERS[1] / n) for n in names} <= copied
    assert t.deleted == [{"hashes": [t.hash], "deleteFiles": ["true"]}]  # the import kept it
