import asyncio
import hashlib
import os
import signal
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from mixsync.acquire.requests import create_request
from mixsync.core.jobs import JobKind, JobState
from mixsync.db.models.discovery import ProvisionalTrack
from mixsync.db.models.library import Track

from .conftest import LEASE, Env
from .fakes import BIG
from .test_pipeline import FIRST, SECOND, library, request, status

pytestmark = pytest.mark.crash


def tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


async def test_e3_kill_mid_import_loses_nothing(env: Env) -> None:
    """Exit criterion 3: SIGKILL between two files of an import; after a restart every file is
    in the library exactly once; the sources are untouched until the import completes."""
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    rid = request(env)
    await env.drain(until=lambda: status(env, rid) == "importing")
    (job,) = env.jobs(JobKind.IMPORT)
    assert job.status is JobState.QUEUED
    sources = tree(env.downloads)

    worker = Path(__file__).with_name("crash_pipeline_worker.py")
    proc = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-I",
            str(worker),
            str(env.data_dir),
            str(env.downloads),
            env.clock.now().isoformat(),
        ],
        env=os.environ,
        capture_output=True,
    )
    assert proc.returncode == -signal.SIGKILL, proc.stderr
    assert status(env, rid) == "importing"
    assert tree(env.downloads) == sources  # nothing is released before the import completes
    assert library(env) == [FIRST, SECOND]  # both published; the second was never completed
    with env.sessions() as s:
        assert [t.path for t in s.scalars(select(Track))] == [FIRST]  # no row for the second

    # restart: recover the journal, let the dead worker's lease expire, run the job again
    env.ops.recover()
    env.clock.advance(LEASE + timedelta(seconds=1))
    await env.drain()

    assert status(env, rid) == "imported"
    assert library(env) == [FIRST, SECOND]  # no " (2)" copies
    assert not list((env.data_dir / ".incoming").iterdir())
    assert tree(env.downloads) == {}  # the resumed import released them
    with env.sessions() as s:
        assert sorted(t.path for t in s.scalars(select(Track))) == [FIRST, SECOND]
    assert env.target.rescans == 1


async def test_kill_mid_discover_import_resumes_without_duplicates(env: Env) -> None:
    env.source.add("peer1", "Music\\Gamma Band\\Gamma Album", BIG)
    wanted = ("Gamma-rec-3", "Gamma-rec-5", "Gamma-rec-7")
    with env.sessions.begin() as s:
        rid = create_request(
            s,
            env.clock.now(),
            env.user_id,
            str(BIG.release_id),
            provisional=True,
            recordings=wanted,
        ).id
    await env.drain(until=lambda: status(env, rid) == "importing")
    sources = tree(env.downloads)

    worker = Path(__file__).with_name("crash_pipeline_worker.py")
    proc = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-I", str(worker), str(env.data_dir), str(env.downloads),
         env.clock.now().isoformat()],
        env=os.environ,
        capture_output=True,
    )  # fmt: skip
    assert proc.returncode == -signal.SIGKILL, proc.stderr
    assert tree(env.downloads) == sources

    env.ops.recover()
    env.clock.advance(LEASE + timedelta(seconds=1))
    await env.drain()

    assert status(env, rid) == "imported"
    assert len(list((env.data_dir / "discover").rglob("*.mp3"))) == 3
    assert not list((env.data_dir / ".incoming").iterdir())
    assert len(tree(env.downloads)) == 7
    with env.sessions() as s:
        assert sorted(r.recording_mbid for r in s.scalars(select(ProvisionalTrack))) == list(wanted)
        assert list(s.scalars(select(Track))) == []
