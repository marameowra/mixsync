"""Adopting unrequested folders in downloads: settle, ownership, release, review, no re-queuing."""

import shutil
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from mixsync.acquire import dropfolder
from mixsync.acquire.adopter import adopter
from mixsync.acquire.requests import candidate_dict
from mixsync.core.jobs import JobKind, JobState
from mixsync.core.matching import Candidate, CandidateFile
from mixsync.db.journal import SqlJournal
from mixsync.db.models.library import Track
from mixsync.db.models.safety import FileOp
from mixsync.db.models.work import Job, Request
from mixsync.db.queue import JobRecord, enqueue_in
from mixsync.library.fileops import FileOps
from mixsync.library.importer import Importer

from .conftest import Env
from .fakes import ALBUM, AUDIO, OTHER
from .test_migration import Meta, put_album
from .test_pipeline import SECOND, decision_rows, library


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
    (job,) = rig.env.jobs(JobKind.VERIFY)
    assert req.origin == {"candidate": job.payload["candidate"], "paths": job.payload["paths"]}


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


async def test_owned_folders_are_never_opened(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    d = put_album(rig.dl, "Alpha Album")
    (d / "bad.flac").write_bytes(b"junk")  # would be reported unreadable if it were read
    rig.add_job(
        JobKind.VERIFY, {"request_id": 1, "paths": [str(d / "01 Song One.mp3")]}, JobState.FAILED
    )
    rig.settle()
    with caplog.at_level("DEBUG"):
        await rig.scan()
    assert rig.requests() == []
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


async def test_corrupt_and_unsupported_files_stay_and_are_reported(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    (rig.dl / "Junk").mkdir()
    (rig.dl / "Junk" / "bad.flac").write_bytes(b"junk")
    (rig.dl / "Junk" / "song.wma").write_bytes(b"wma")
    rig.settle()
    with caplog.at_level("WARNING"):
        await rig.scan()
    assert rig.requests() == []
    assert sorted(p.name for p in rig.files()) == ["bad.flac", "song.wma"]
    (rec,) = [r for r in caplog.records if "unreadable" in r.getMessage()]
    assert "2 unreadable" in rec.getMessage() and "bad.flac" in rec.getMessage()


async def test_an_owned_recording_is_dropped_and_its_file_stays(rig: Rig) -> None:
    put_album(rig.dl, "Alpha Album", mbid=str(ALBUM.release_id))
    with rig.env.sessions.begin() as s:
        s.add(
            Track(
                path="elsewhere.mp3",
                recording_mbid="Alpha-rec-1",
                release_group_mbid=ALBUM.release_group_id,
                status="verified",
                batch_id="old",
                imported_at=rig.env.clock.now(),
            )
        )
    rig.settle()
    await rig.scan()
    await rig.env.drain()
    assert library(rig.env) == [SECOND]
    assert [p.name for p in rig.files()] == ["01 Song One.mp3"]


async def test_a_download_that_starts_during_the_scan_is_not_adopted(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    put_album(rig.dl, "Alpha Album")
    rig.settle()
    real = dropfolder.scan

    def scan_then_download(*args: Any) -> dict[str, Any]:
        found = real(*args)
        files = (CandidateFile("@@share\\Music\\Alpha Album\\01.mp3", 1, "mp3"),)
        cand = Candidate("peer", "@@share\\Music\\Alpha Album", files)
        rig.add_job(
            JobKind.DOWNLOAD,
            {"request_id": 1, "candidate": candidate_dict(cand)},
            JobState.RUNNING,
        )
        return found

    monkeypatch.setattr(dropfolder, "scan", scan_then_download)
    await rig.scan()
    assert rig.requests() == []


async def test_a_disc_folder_owned_by_a_download_keeps_the_rest_of_its_album_out(
    rig: Rig,
) -> None:
    put_album(rig.dl, "CD1")  # MixSync's download of some other album
    put_album(rig.dl, "CD2", OTHER)  # the other disc of a hand download that shares the parent
    files = (CandidateFile("@@share\\Music\\Other\\CD1\\x.mp3", 1, "mp3"),)
    cand = Candidate("peer", "@@share\\Music\\Other\\CD1", files)
    rig.add_job(
        JobKind.DOWNLOAD, {"request_id": 1, "candidate": candidate_dict(cand)}, JobState.FAILED
    )
    rig.settle()
    await rig.scan()
    assert rig.requests() == []


async def test_a_changed_folder_is_queued_again_after_the_cap(rig: Rig) -> None:
    d = put_album(rig.dl, "Alpha Album")
    rig.settle()
    for n in range(1, 5):  # a failed adoption is tried again, but only three times
        await rig.scan()
        assert len(rig.requests()) == min(n, 3)
        with rig.env.sessions.begin() as s:
            s.execute(update(Request).values(status="failed"))
    shutil.copy(AUDIO / "cc0-tone-01.mp3", d / "03 Extra.mp3")
    rig.settle()
    await rig.scan()
    assert len(rig.requests()) == 4
