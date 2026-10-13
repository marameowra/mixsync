"""Run by test_tag_undo.py in a subprocess: restore a file's tags and SIGKILL at the given step.

usage: python -I crash_tag_undo_worker.py <step> <data_dir> <rel>
"""

import os
import signal
import sys
from pathlib import Path

from mixsync.core.clock import SystemClock
from mixsync.core.config import Settings
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.journal import SqlJournal
from mixsync.library import fileops, tag_undo, tags
from mixsync.library.fileops import FileOps

step, data_dir, rel = sys.argv[1:]


def hook(name: str) -> None:
    if name == step:
        os.kill(os.getpid(), signal.SIGKILL)


sessions = make_session_factory(make_engine(Settings()))
clock = SystemClock()
fo = FileOps(Path(data_dir), Path(data_dir) / "dl", SqlJournal(sessions, clock), clock)
old = tags.read_all(fo.library / rel)  # any valid snapshot will do
fileops._hook = hook  # pyright: ignore[reportPrivateUsage]
tag_undo.restore_tags(fo, sessions, clock, 1, rel, old)
