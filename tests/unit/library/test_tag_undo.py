import hashlib
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import SystemClock
from mixsync.db.journal import SqlJournal
from mixsync.library import tag_undo
from mixsync.library.fileops import FileOps
from mixsync.library.purge import TrashPurger

from .conftest import MakeSrc

pytestmark = pytest.mark.crash

REL = "Artist/Album/01 Song.flac"


@pytest.mark.parametrize("step", ["tag", "prerename"])
def test_crash_mid_restore_is_recovered(
    step: str,
    ops: FileOps,
    data_dir: Path,
    make_src: MakeSrc,
    journal: SqlJournal,
    sessions: sessionmaker[Session],
) -> None:
    ops.import_file(make_src(), REL, lambda p, _op: None, "import")
    original = hashlib.sha256((data_dir / "library" / REL).read_bytes()).hexdigest()
    worker = Path(__file__).with_name("crash_tag_undo_worker.py")
    proc = subprocess.run(
        [sys.executable, "-I", str(worker), step, str(data_dir), REL],
        env=os.environ,
        capture_output=True,
    )
    assert proc.returncode == -signal.SIGKILL, proc.stderr
    assert not ops.has(REL)  # the window: the original is only in the trash

    purger = TrashPurger(sessions, SystemClock(), ops, 0)
    assert len(purger.protected()) == 1  # and the purge must leave it alone

    ops.recover()
    tag_undo.recover(sessions, ops)

    assert hashlib.sha256((data_dir / "library" / REL).read_bytes()).hexdigest() == original
    assert purger.protected() == set()
    assert journal.pending() == []
