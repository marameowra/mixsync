"""Adopting unrequested folders in downloads: settle, ownership, release, review, no re-queuing."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from mixsync.acquire.adopter import adopter
from mixsync.acquire.requests import candidate_dict
from mixsync.core.jobs import JobKind, JobState
from mixsync.core.matching import Candidate, CandidateFile
from mixsync.db.journal import SqlJournal
from mixsync.db.models.safety import FileOp
from mixsync.db.models.work import Job, Request
from mixsync.db.queue import JobRecord, enqueue_in
from mixsync.library.fileops import FileOps
from mixsync.library.importer import Importer

from .conftest import Env
from .fakes import ALBUM
from .test_migration import Meta, put_album
from .test_pipeline import decision_rows, library


class Rig:
    def __init__(self, env: Env, tmp_path: Path, enabled: bool = True) -> None:
        self.env, self.dl = env, env.downloads
        self.meta = Meta()
        ops = FileOps(env.data_dir, env.downloads, SqlJournal(env.sessions, env.clock), env.clock)
        env.pipeline = replace(
            env.pipeline,
            metadata=self.meta,
            importer=Importer(ops, env.sessions, env.clock, env.settings.path_template),
        )
        env.handlers = env.pipeline.handlers()
        self.adopt = adopter(
            env.sessions, env.clock, self.dl, tmp_path / "config" / "adopt", enabled, 10
        )
        self.n = 0

    def settle(self) -> None:
        self.env.clock.advance(timedelta(minutes=15))

    async def scan(self) -> None:
        self.adopt.work_dir.mkdir(parents=True, exist_ok=True)
        self.n += 1
        now = self.env.clock.now()
        job = JobRecord(
            self.n, JobKind.ADOPT_SCAN, {}, JobState.RUNNING, 1, 5, "w", now, None, None
        )
        await self.adopt.run(job)
        with self.env.sessions.begin() as s:  # the chain's next scan is not part of these tests
            s.execute(
                update(Job).where(Job.kind == JobKind.ADOPT_SCAN).values(status=JobState.CANCELLED)
            )

    def requests(self) -> list[Request]:
        with self.env.sessions() as s:
            return list(s.scalars(select(Request).order_by(Request.id)))

    def add_job(self, kind: JobKind, payload: dict[str, Any], state: JobState) -> None:
        with self.env.sessions.begin() as s:
            enqueue_in(s, self.env.clock.now(), kind, payload)
            s.execute(
                update(Job)
                .where(Job.kind == kind, Job.status == JobState.QUEUED)
                .values(status=state)
            )

    def files(self) -> list[Path]:
        return sorted(p for p in self.dl.rglob("*") if p.is_file())


@pytest.fixture
def rig(env: Env, tmp_path: Path) -> Rig:
    return Rig(env, tmp_path)


async def test_a_hand_made_folder_is_queued_only_after_it_settles(rig: Rig) -> None:
    put_album(rig.dl, "Alpha Album")
    rig.env.clock.advance(timedelta(minutes=5))  # quiet for less than the 10 minute window
    await rig.scan()
    assert rig.requests() == []
    rig.settle()
    await rig.scan()
    (req,) = rig.requests()
    assert (req.source, req.status) == ("adopt", "verifying")
    assert len(rig.env.jobs(JobKind.VERIFY)) == 1


def test_off_by_default_schedules_nothing(env: Env, tmp_path: Path) -> None:
    Rig(env, tmp_path, enabled=False).adopt.start_chains()
    assert env.jobs(JobKind.ADOPT_SCAN) == []
    Rig(env, tmp_path).adopt.start_chains()
    assert len(env.jobs(JobKind.ADOPT_SCAN)) == 1


async def test_a_folder_of_a_live_download_is_skipped(rig: Rig) -> None:
    put_album(rig.dl, "Alpha Album")
    files = (CandidateFile("@@share\\Music\\Alpha Album\\01.mp3", 1, "mp3"),)
    cand = Candidate("peer", "@@share\\Music\\Alpha Album", files)
    rig.add_job(
        JobKind.DOWNLOAD, {"request_id": 1, "candidate": candidate_dict(cand)}, JobState.RUNNING
    )
    rig.settle()
    await rig.scan()
    assert rig.requests() == []


async def test_a_folder_a_finished_verify_job_used_is_skipped(rig: Rig) -> None:
    d = put_album(rig.dl, "Alpha Album")
    paths = [str(p) for p in sorted(d.iterdir())]
    rig.add_job(JobKind.VERIFY, {"request_id": 1, "paths": paths}, JobState.FAILED)
    rig.settle()
    await rig.scan()
    assert rig.requests() == []


async def test_a_folder_of_a_rejected_candidate_in_tried_is_skipped(rig: Rig) -> None:
    put_album(rig.dl, "Alpha Album")
    with rig.env.sessions.begin() as s:
        s.add(
            Request(
                user_id=rig.env.user_id,
                release_mbid="x",
                artist="a",
                album="b",
                status="failed",
                tried=[["peer", "@@share\\Music\\Alpha Album"]],
            )
        )
    rig.settle()
    await rig.scan()
    assert [r.source for r in rig.requests()] == ["manual"]  # only the one added above


async def test_a_copy_source_folder_is_skipped(rig: Rig) -> None:
    d = put_album(rig.dl, "Alpha Album")
    with rig.env.sessions.begin() as s:
        s.add(
            FileOp(
                batch_id="b",
                kind="copy",
                src=str(next(d.iterdir())),
                dst="x",
                status="done",
                started_at=rig.env.clock.now(),
            )
        )
    rig.settle()
    await rig.scan()
    assert rig.requests() == []


async def test_unreadable_files_stay_and_the_same_folder_is_not_queued_twice(rig: Rig) -> None:
    put_album(rig.dl, "Alpha Album")
    (rig.dl / "notes.txt").write_text("not audio")
    rig.settle()
    await rig.scan()
    await rig.scan()
    assert len(rig.requests()) == 1 and len(rig.env.jobs(JobKind.VERIFY)) == 1
    assert (rig.dl / "notes.txt").exists()


async def test_a_verified_import_releases_the_downloads(rig: Rig) -> None:
    put_album(rig.dl, "Alpha Album", mbid=str(ALBUM.release_id))
    rig.settle()
    await rig.scan()
    await rig.env.drain()
    (req,) = rig.requests()
    assert (req.status, req.source) == ("imported", "adopt")
    assert len(library(rig.env)) == 2
    assert rig.files() == []


async def test_a_reject_goes_to_review_without_a_search(rig: Rig) -> None:
    put_album(rig.dl, "Alpha Album")
    rig.settle()
    await rig.scan()
    await rig.env.drain()
    (req,) = rig.requests()
    (d,) = decision_rows(rig.env, 2)
    assert (req.status, d.status) == ("review", "pending_review")
    assert rig.env.jobs(JobKind.SEARCH) == [] and len(rig.files()) == 2
