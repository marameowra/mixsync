"""A person's reject of a review candidate re-files its quarantined files into
quarantine/rejected/<today>, where the daily purge reaches them (#244)."""

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mixsync.acquire.evidence import Evidence
from mixsync.core.jobs import JobKind
from mixsync.db.models.safety import FileOp
from mixsync.db.queue import JobRecord
from mixsync.library import fileops

from .conftest import Env, login
from .fakes import ALBUM
from .test_pipeline import decision_rows, quarantined, request, status, veto_second_file
from .test_quarantine import file_ops

QUARANTINE = "quarantine"


async def in_review(env: Env) -> int:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album", ALBUM)
    veto_second_file(env)
    rid = request(env)
    await env.drain()
    assert status(env, rid) == "review" and len(quarantined(env, "review")) == 2
    (d,) = decision_rows(env, 2)
    return d.id


def reject(web: TestClient, did: int) -> None:
    tok = login(web, "alice")
    assert web.post(f"/review/{did}/reject", data={"csrf_token": tok}).status_code == 303


async def run_refile(env: Env) -> JobRecord:
    """Run jobs up to and including the refile (the dropped candidate's search goes on forever)."""
    while (job := await env.step()) and job.kind != JobKind.REFILE:
        pass
    assert job
    return job


def refile_moves(env: Env) -> list[FileOp]:
    return [o for o in file_ops(env, "copy") + file_ops(env, "release") if "refile" in o.batch_id]


async def test_a_reject_refiles_the_files_into_rejected_today(env: Env, web: TestClient) -> None:
    did = await in_review(env)
    before = {p.name: p.read_bytes() for p in quarantined(env, "review")}
    reject(web, did)
    await run_refile(env)

    day = env.data_dir / QUARANTINE / "rejected" / env.clock.now().date().isoformat()
    kept = quarantined(env, "rejected")
    assert quarantined(env, "review") == []
    assert {p.name: p.read_bytes() for p in kept} == before
    assert all(p.is_relative_to(day) for p in kept)
    (d,) = decision_rows(env, 2)
    assert d.final_action == "reject"
    ev = Evidence.parse(d.evidence)
    assert {Path(p) for p in ev.paths} == set(kept)
    assert {Path(a["local_path"]) for a in ev.assignment} <= set(kept)
    moves = refile_moves(env)
    assert len(moves) == 4 and {o.status for o in moves} == {"done"}
    assert {o.root for o in moves} == {QUARANTINE}


async def test_refiled_files_are_purged_after_the_trash_window(env: Env, web: TestClient) -> None:
    did = await in_review(env)
    reject(web, did)
    await run_refile(env)
    assert env.ops.purge_quarantine(30, "p") == 0  # inside the window
    env.clock.advance(timedelta(days=31))
    assert env.ops.purge_quarantine(30, "p") == 2
    assert quarantined(env, "rejected") == []


async def test_running_the_job_again_changes_nothing(env: Env, web: TestClient) -> None:
    did = await in_review(env)
    reject(web, did)
    job = await run_refile(env)
    kept = quarantined(env, "rejected")
    n = len(file_ops(env, "copy"))
    await env.pipeline.refile(job)
    assert quarantined(env, "rejected") == kept and len(file_ops(env, "copy")) == n


async def test_an_interrupted_move_recovers_without_loss_or_duplicates(
    env: Env, web: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    did = await in_review(env)
    before = {p.name: p.read_bytes() for p in quarantined(env, "review")}
    real = fileops._hook

    def die(step: str) -> None:
        if step == "release":
            raise KeyboardInterrupt  # dies with the release op pending

    monkeypatch.setattr(fileops, "_hook", die)
    reject(web, did)
    with pytest.raises(KeyboardInterrupt):
        await run_refile(env)
    monkeypatch.setattr(fileops, "_hook", real)

    env.ops.recover()  # settles the pending release: the source is still there
    env.clock.advance(timedelta(hours=1))
    await run_refile(env)

    assert quarantined(env, "review") == []
    kept = quarantined(env, "rejected")
    assert {p.name: p.read_bytes() for p in kept} == before and len(kept) == 2
    (d,) = decision_rows(env, 2)
    assert {Path(p) for p in Evidence.parse(d.evidence).paths} == set(kept)


def test_files_outside_quarantine_review_are_never_moved(env: Env, tmp_path: Path) -> None:
    dl = env.downloads / "Album" / "01.mp3"
    dl.parent.mkdir()
    dl.write_bytes(b"audio")
    old = tmp_path / "library-old" / "01.mp3"
    old.parent.mkdir()
    old.write_bytes(b"audio")
    done = env.data_dir / QUARANTINE / "rejected" / "2026-01-01" / "A" / "01.mp3"
    done.parent.mkdir(parents=True)
    done.write_bytes(b"audio")
    gone = env.data_dir / QUARANTINE / "review" / "2026-01-01" / "A" / "gone.mp3"
    for p in (dl, old, done, gone):
        assert env.ops.refile_rejected(p, "b") == p
    assert dl.read_bytes() == old.read_bytes() == done.read_bytes() == b"audio"
    assert file_ops(env, "copy") == [] and file_ops(env, "release") == []


async def test_accept_leaves_the_files_to_the_import(env: Env, web: TestClient) -> None:
    did = await in_review(env)
    tok = login(web, "alice")
    assert web.post(f"/review/{did}/accept", data={"csrf_token": tok}).status_code == 303
    await env.drain()
    assert quarantined(env, "rejected") == [] and quarantined(env, "review") == []
    assert env.jobs(JobKind.REFILE) == []
