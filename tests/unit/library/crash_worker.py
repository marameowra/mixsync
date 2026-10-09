"""Run by test_crash.py in a subprocess: import one file and SIGKILL itself at the given step.

usage: python -I crash_worker.py <step> <data_dir> <src> <final_rel>
"""

import os
import signal
import sys
from pathlib import Path

from mixsync.core.clock import SystemClock
from mixsync.core.config import Settings
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.journal import SqlJournal
from mixsync.library import fileops, tags
from mixsync.library.fileops import FileOps

step, data_dir, src, rel = sys.argv[1:]


def hook(name: str) -> None:
    if name == step:
        os.kill(os.getpid(), signal.SIGKILL)


def write_tags(path: Path, _op_id: int) -> None:
    tags.write(path, tags.TagSet("T", "A", "Al", "AA", 1, 1))


fileops._hook = hook  # pyright: ignore[reportPrivateUsage]
journal = SqlJournal(make_session_factory(make_engine(Settings())), SystemClock())
FileOps(Path(data_dir), journal, SystemClock()).import_file(Path(src), rel, write_tags, "crash")
