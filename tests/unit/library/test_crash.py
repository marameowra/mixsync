import hashlib
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.db.journal import SqlJournal
from mixsync.db.models.safety import FileOp
from mixsync.library.fileops import FileOps

from .conftest import MakeSrc
from .test_fileops import append_tag, files

pytestmark = pytest.mark.crash

REL = "Artist/Album (2000)/01-01 Song.flac"
STEPS = ["copy", "fsync", "tag", "prerename", "rename"]  # fileops._hook points, in order


@pytest.mark.parametrize("step", STEPS)
def test_sigkill_at_step(
    step: str,
    ops: FileOps,
    data_dir: Path,
    make_src: MakeSrc,
    journal: SqlJournal,
    sessions: sessionmaker[Session],
) -> None:
    src = make_src()
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    worker = Path(__file__).with_name("crash_worker.py")
    proc = subprocess.run(
        [sys.executable, "-I", str(worker), step, str(data_dir), str(src), REL],
        env=os.environ,
        capture_output=True,
    )
    assert proc.returncode == -signal.SIGKILL, proc.stderr

    final = data_dir / "library" / REL
    assert final.exists() == (step == "rename")  # only a complete, renamed file is ever visible
    assert files(data_dir / "library") == ([REL] if step == "rename" else [])
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before
    assert [o.status for o in journal.pending()] == ["started"]

    ops.recover()

    assert journal.pending() == []
    assert files(data_dir / ".incoming") == []
    with sessions() as s:
        (op,) = s.scalars(select(FileOp)).all()
    assert op.status == ("done" if step == "rename" else "rolled_back")
    if step == "rename":
        assert op.dst_hash == hashlib.sha256(final.read_bytes()).hexdigest()
    else:
        assert not final.exists() and files(data_dir / ".trash")  # leftover kept, not deleted

    # a re-run finishes the job, or finds it already done and refuses to overwrite it
    if step == "rename":
        with pytest.raises(FileExistsError):
            ops.import_file(src, REL, append_tag, "again")
    else:
        ops.import_file(src, REL, append_tag, "again")
        assert final.exists()
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before
    assert files(data_dir / ".incoming") == []
