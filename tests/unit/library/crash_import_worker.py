"""Run by test_crash.py: import_release, SIGKILLing itself right after the file is imported but
before the tracks row is written.

usage: python -I crash_import_worker.py <data_dir> <src>...
"""

import os
import signal
import sys
from pathlib import Path

from mixsync.core.clock import SystemClock
from mixsync.core.config import Settings
from mixsync.core.matching import AlbumInfo, TrackInfo
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.journal import SqlJournal
from mixsync.library.fileops import FileOps
from mixsync.library.importer import Importer


def die(*_a: object) -> None:
    os.kill(os.getpid(), signal.SIGKILL)


settings, clock = Settings(), SystemClock()
sessions = make_session_factory(make_engine(settings))
ops = FileOps(Path(sys.argv[1]), SqlJournal(sessions, clock), clock)
importer = Importer(ops, sessions, clock, settings.path_template)
importer._ensure_row = die  # pyright: ignore[reportPrivateUsage]
tracks = [
    (Path(p), TrackInfo(f"T{i}", medium_index=i), None) for i, p in enumerate(sys.argv[2:], 1)
]
importer.import_release(tracks, AlbumInfo("A", "B", (), year=2000), "verified", "crash")
