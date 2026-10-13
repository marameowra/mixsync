import hashlib
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import SystemClock
from mixsync.db.journal import SqlJournal
from mixsync.db.models.safety import FileOp
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


def test_trash_op_never_completed_is_still_protected(
    ops: FileOps, make_src: MakeSrc, sessions: sessionmaker[Session]
) -> None:
    # the web process died inside the trash, before its journal row was completed
    ops.import_file(make_src(), REL, lambda p, _op: None, "import")
    ops.trash(REL, f"{tag_undo.BATCH}x")
    with sessions.begin() as s:
        s.execute(update(FileOp).where(FileOp.kind == "trash").values(status="started"))
    purger = TrashPurger(sessions, SystemClock(), ops, 0)
    assert len(purger.protected()) == 1
    ops.recover()  # the worker settles the row, then puts the original back
    tag_undo.recover(sessions, ops)
    assert ops.has(REL) and purger.protected() == set()


def test_rolled_back_restore_is_not_redone_after_the_track_moved(
    ops: FileOps, data_dir: Path, make_src: MakeSrc, sessions: sessionmaker[Session]
) -> None:
    # a failed restore put the original back; later the track moved off REL
    ops.import_file(make_src(), REL, lambda p, _op: None, "import")
    batch = f"{tag_undo.BATCH}y"
    trashed = ops.trash(REL, batch)
    ops.restore(trashed, REL, batch)
    (data_dir / "library" / REL).unlink()
    assert not ops.has(REL)

    tag_undo.recover(sessions, ops)  # must not raise
    assert not ops.has(REL)
