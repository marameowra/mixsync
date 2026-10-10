"""Run by test_crash.py: claim the due IMPORT job and run it, SIGKILLing this process at the
SECOND file's publish ("rename").

usage: python -I crash_pipeline_worker.py <data_dir> <downloads_dir> <now, ISO 8601>
"""

import asyncio
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # -I leaves the script's directory off the path

from fakes import (  # noqa: E402
    FakeAcoustId,
    FakeCoverArt,
    FakeMetadata,
    FakeSource,
    FakeTarget,
    FixedClock,
    make_fileops,
    make_pipeline,
)

from mixsync.core.config import Settings  # noqa: E402
from mixsync.core.jobs import DEFAULT_LEASE  # noqa: E402
from mixsync.db.engine import make_engine, make_session_factory  # noqa: E402
from mixsync.db.queue import JobQueue  # noqa: E402
from mixsync.library import fileops  # noqa: E402
from mixsync.worker import run_job  # noqa: E402

data_dir, downloads = Path(sys.argv[1]), Path(sys.argv[2])
settings, clock = Settings(), FixedClock(datetime.fromisoformat(sys.argv[3]))
engine = make_engine(settings)
sessions = make_session_factory(engine)
pipeline = make_pipeline(
    sessions,
    clock,
    make_fileops(sessions, clock, data_dir),
    settings.path_template,
    FakeSource(downloads, data_dir),
    FakeAcoustId(),
    FakeMetadata(),
    FakeCoverArt(),
    FakeTarget(),
)
renames = 0


def hook(step: str) -> None:
    global renames
    if step == "rename":
        renames += 1
        if renames == 2:
            os.kill(os.getpid(), signal.SIGKILL)


fileops._hook = hook  # pyright: ignore[reportPrivateUsage]
queue = JobQueue(engine, clock)
job = queue.claim("doomed", DEFAULT_LEASE)
assert job, "no due job"
asyncio.run(run_job(queue, job, "doomed", pipeline.handlers(), DEFAULT_LEASE))
