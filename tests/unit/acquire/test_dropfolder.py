"""Drop folder: pickup, banding, originals to the trash, no re-queuing, no Soulseek search."""

import os
import shutil
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from mixsync.acquire import dropfolder, verify
from mixsync.acquire.dropfolder import DropFolder
from mixsync.acquire.pipeline import Pipeline
from mixsync.acquire.requests import create_request
from mixsync.app import create_app
from mixsync.core.clock import aware
from mixsync.core.config import Settings
from mixsync.core.jobs import JobKind, JobState
from mixsync.db.journal import SqlJournal
from mixsync.db.models.work import Job, Request
from mixsync.db.queue import JobRecord
from mixsync.library.fileops import FileOps
from mixsync.library.importer import Importer

from .conftest import Env, login
from .fakes import ALBUM, AUDIO, OTHER
from .test_migration import Meta, put_album
from .test_pipeline import decision_rows, library


class Rig:
    def __init__(self, env: Env, tmp_path: Path) -> None:
        self.env, self.drop = env, tmp_path / "drop"
        self.drop.mkdir()
        self.meta = Meta()
        ops = FileOps(
            env.data_dir, env.downloads, SqlJournal(env.sessions, env.clock), env.clock, self.drop
        )
        env.pipeline = replace(
            env.pipeline,
            metadata=self.meta,
            importer=Importer(ops, env.sessions, env.clock, env.settings.path_template),
        )
        env.handlers = env.pipeline.handlers()
        self.dropper = DropFolder(env.sessions, env.clock, self.drop, tmp_path / "config" / "drop")
        self.n = 0

    def settle(self) -> None:
        """Let the files age past the settle time (utime would bump ctime, so move the clock)."""
        self.env.clock.advance(timedelta(minutes=5))

    def job(self) -> JobRecord:
        self.n += 1
        now = self.env.clock.now()
        return JobRecord(
            self.n, JobKind.DROP_SCAN, {}, JobState.RUNNING, 1, 5, "w", now, None, None
        )

    async def scan(self) -> None:
        self.dropper.work_dir.mkdir(parents=True, exist_ok=True)
        await self.dropper.run(self.job())
        with self.env.sessions.begin() as s:  # the chain's next scan is not part of these tests
            s.execute(
                update(Job).where(Job.kind == JobKind.DROP_SCAN).values(status=JobState.CANCELLED)
            )

    def requests(self) -> list[Request]:
        with self.env.sessions() as s:
            return list(s.scalars(select(Request).order_by(Request.id)))

    def files(self) -> list[Path]:
        return sorted(p for p in self.drop.rglob("*") if p.is_file())

    def trashed(self) -> list[Path]:
        return sorted((self.env.data_dir / ".trash").rglob("*.mp3"))


@pytest.fixture
def rig(env: Env, tmp_path: Path) -> Rig:
    return Rig(env, tmp_path)


@pytest.fixture
def drop_web(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MIXSYNC_DATA_DIR", str(rig.env.data_dir))
    monkeypatch.setenv("MIXSYNC_DOWNLOADS_DIR", str(rig.env.downloads))
    monkeypatch.setenv("MIXSYNC_DROP_DIR", str(rig.drop))
    return TestClient(create_app(rig.env.sessions, Settings()), follow_redirects=False)


async def test_tagged_drop_imports_and_originals_go_to_the_trash(rig: Rig) -> None:
    put_album(rig.drop, "Alpha Band/Alpha Album", mbid=str(ALBUM.release_id))
    rig.settle()
    await rig.scan()
    await rig.env.drain()

    (req,) = rig.requests()
    assert (req.status, req.source, req.release_mbid) == ("imported", "drop", ALBUM.release_id)
    assert len(library(rig.env)) == 2
    assert rig.files() == []  # the folder emptied itself
    trashed = rig.trashed()
    assert len(trashed) == 2 and all(".drop" in p.parts for p in trashed)
    assert rig.meta.searches == [] and rig.env.jobs(JobKind.SEARCH) == []


async def test_untagged_drop_goes_to_review_and_reject_keeps_files_and_does_not_search(
    rig: Rig, drop_web: TestClient
) -> None:
    put_album(rig.drop, "Alpha Band/Alpha Album")
    rig.settle()
    await rig.scan()
    await rig.env.drain()

    (req,) = rig.requests()
    (d,) = decision_rows(rig.env, 2)
    assert (req.status, d.status) == ("review", "pending_review")

    with drop_web as web:
        assert web.get(f"/media/preview/{d.id}/0").status_code == 303  # not logged in
        tok = login(web, "alice")
        assert web.get(f"/media/preview/{d.id}/0").status_code == 200  # plays from the drop dir
        assert web.post(f"/review/{d.id}/reject", data={"csrf_token": tok}).status_code == 303
    await rig.env.drain()
    (req,) = rig.requests()
    assert (req.status, req.last_error) == ("skipped", "rejected in review")
    assert rig.env.jobs(JobKind.SEARCH) == [] and rig.env.source.queries == []
    assert len(rig.files()) == 2 and rig.trashed() == []

    await rig.scan()  # unchanged: not queued again
    await rig.env.drain()
    assert len(rig.requests()) == 1

    shutil.copy(
        AUDIO / "cc0-tone-01.mp3", rig.drop / "Alpha Band/Alpha Album/03 Extra.mp3"
    )  # changed: again
    rig.settle()
    await rig.scan()
    assert len(rig.requests()) == 2


async def test_files_still_being_copied_are_left_for_the_next_scan(rig: Rig) -> None:
    put_album(rig.drop, "Alpha Band/Alpha Album")  # modified just now
    await rig.scan()
    assert rig.requests() == []
    rig.env.clock.advance(timedelta(minutes=2))
    await rig.scan()
    assert len(rig.requests()) == 1


async def test_a_second_scan_does_not_queue_the_same_album_twice(rig: Rig) -> None:
    put_album(rig.drop, "Alpha Band/Alpha Album")
    (rig.drop / "notes.txt").write_text("not audio")
    rig.settle()
    await rig.scan()
    await rig.scan()
    assert len(rig.requests()) == 1 and len(rig.env.jobs(JobKind.VERIFY)) == 1
    assert (rig.drop / "notes.txt").exists()


async def test_a_dropped_album_starts_a_verify_that_identifies_the_release(rig: Rig) -> None:
    put_album(rig.drop, "Alpha Band/Alpha Album")
    rig.settle()
    await rig.scan()
    (req,) = rig.requests()
    (job,) = rig.env.jobs(JobKind.VERIFY)
    p = job.payload
    assert (p["request_id"], p["release_mbid"]) == (req.id, None)
    assert len(p["paths"]) == len(ALBUM.tracks) and all(Path(x).is_absolute() for x in p["paths"])
    assert job.max_attempts == verify.VERIFY_ATTEMPTS and job.idempotency_key
    assert req.status == "verifying"
    assert req.origin == {"candidate": p["candidate"], "paths": p["paths"]}
    await rig.env.drain()
    assert [d.release_given for d in decision_rows(rig.env, 2)] == [False]  # identified


async def test_a_scan_schedules_the_next_one(rig: Rig) -> None:
    await rig.dropper.run(rig.job())
    (nxt,) = rig.env.jobs(JobKind.DROP_SCAN)
    assert aware(nxt.run_after) > rig.env.clock.now()


def test_no_drop_dir_schedules_nothing(env: Env, tmp_path: Path) -> None:
    DropFolder(env.sessions, env.clock, None, tmp_path).start_chains()
    assert env.jobs(JobKind.DROP_SCAN) == []
    DropFolder(env.sessions, env.clock, tmp_path, tmp_path).start_chains()
    assert len(env.jobs(JobKind.DROP_SCAN)) == 1


async def test_an_old_mtime_with_a_fresh_ctime_is_not_quiet(rig: Rig) -> None:
    put_album(rig.drop, "Alpha Band/Alpha Album")
    rig.settle()
    real = Path.stat
    fresh = rig.env.clock.now().timestamp()

    def stat(p: Path) -> os.stat_result:
        st = real(p)
        return os.stat_result((*st[:8], int(st.st_mtime), int(fresh)))

    rig.dropper = replace(rig.dropper, stat=stat)
    await rig.scan()
    assert rig.requests() == []


async def test_a_file_vanishing_mid_scan_only_delays_its_album(rig: Rig) -> None:
    put_album(rig.drop, "Alpha Band/Alpha Album")
    put_album(rig.drop, "Beta Band/Beta Album", OTHER)
    rig.settle()
    real = Path.stat

    def stat(p: Path) -> os.stat_result:
        if p.name == "01 Song One.mp3":
            raise FileNotFoundError(p)
        return real(p)

    rig.dropper = replace(rig.dropper, stat=stat)
    await rig.scan()
    assert [r.album for r in rig.requests()] == ["Beta Album"]


async def test_a_failed_album_is_queued_again_but_a_settled_one_is_not(rig: Rig) -> None:
    put_album(rig.drop, "Alpha Band/Alpha Album")
    rig.settle()
    await rig.scan()
    await rig.scan()
    assert len(rig.requests()) == 1
    with rig.env.sessions.begin() as s:
        s.execute(update(Request).values(status="failed"))
    await rig.scan()
    reqs = rig.requests()
    assert [r.status for r in reqs] == ["failed", "verifying"]
    await rig.scan()  # the new attempt is live: not queued a third time
    assert len(rig.requests()) == 2


def _delete_verify_jobs(rig: Rig) -> None:
    with rig.env.sessions.begin() as s:
        s.execute(delete(Job).where(Job.kind == JobKind.VERIFY))


async def test_a_new_album_gets_a_content_key_and_a_searched_request_none(rig: Rig) -> None:
    put_album(rig.drop, "Alpha Band/Alpha Album")
    rig.settle()
    await rig.scan()
    with rig.env.sessions.begin() as s:
        create_request(s, rig.env.clock.now(), rig.env.user_id, "x")
    drop, searched = rig.requests()
    assert drop.content_key is not None and drop.content_key.startswith("drop:")
    assert searched.content_key is None


async def test_attempts_are_counted_from_requests_not_verify_jobs(rig: Rig) -> None:
    put_album(rig.drop, "Alpha Band/Alpha Album")
    rig.settle()
    await rig.scan()
    _delete_verify_jobs(rig)
    await rig.scan()  # latest in progress: skipped
    assert len(rig.requests()) == 1
    with rig.env.sessions.begin() as s:
        s.execute(update(Request).values(status="failed"))
    await rig.scan()  # latest failed, under the cap: queued again
    assert [r.status for r in rig.requests()] == ["failed", "verifying"]
    with rig.env.sessions.begin() as s:
        s.execute(update(Request).values(status="failed"))
    _delete_verify_jobs(rig)
    await rig.scan()  # the third attempt
    reqs = rig.requests()
    assert len(reqs) == dropfolder.MAX_ATTEMPTS and reqs[-1].status == "verifying"
    with rig.env.sessions.begin() as s:
        s.execute(update(Request).values(status="failed"))
    _delete_verify_jobs(rig)
    await rig.scan()  # three requests, latest failed: given up
    assert len(rig.requests()) == dropfolder.MAX_ATTEMPTS


async def test_deleting_the_only_request_makes_the_next_scan_queue_again(rig: Rig) -> None:
    put_album(rig.drop, "Alpha Band/Alpha Album")
    rig.settle()
    await rig.scan()
    with rig.env.sessions.begin() as s:
        s.execute(delete(Job).where(Job.kind == JobKind.VERIFY))
        s.execute(delete(Request))
    await rig.scan()
    assert len(rig.requests()) == 1


async def test_an_unusable_file_fails_the_drop_without_a_search(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unusable(_self: object, _path: Path) -> str:
        return "unsupported audio: bad.wav is not readable audio"

    monkeypatch.setattr(Pipeline, "_unusable", unusable)
    put_album(rig.drop, "Alpha Band/Alpha Album")
    rig.settle()
    await rig.scan()
    await rig.env.drain()
    (req,) = rig.requests()
    assert (req.status, req.last_error) == (
        "failed",
        "unsupported audio: bad.wav is not readable audio",
    )
    assert rig.env.jobs(JobKind.SEARCH) == []
