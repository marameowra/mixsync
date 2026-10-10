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

from mixsync.core.jobs import JobKind, JobState
from mixsync.db.models.library import Track

from .conftest import LEASE, Env
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
    in the library exactly once and the sources are untouched."""
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
    assert tree(env.downloads) == sources
    with env.sessions() as s:
        assert sorted(t.path for t in s.scalars(select(Track))) == [FIRST, SECOND]
    assert env.target.rescans == 1
