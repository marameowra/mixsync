import logging
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from mixsync.db.journal import SqlJournal
from mixsync.library import fileops, tag_undo
from mixsync.library.fileops import FileOps

from .conftest import MakeSrc

REL = "Artist/Album/01 Song.flac"


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

    assert tag_undo.unfinished(sessions, ops) == []  # the done restore settles the batch
    tag_undo.recover(sessions, ops)  # must not raise
    assert not ops.has(REL)


def test_restore_settled_by_recovery_mid_flight_still_ends_done(
    ops: FileOps,
    data_dir: Path,
    make_src: MakeSrc,
    sessions: sessionmaker[Session],
    journal: SqlJournal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fileops, "_noreplace", lambda _a, _b: False)  # pyright: ignore[reportPrivateUsage]
    ops.import_file(make_src(), REL, lambda p, _op: None, "import")
    batch = f"{tag_undo.BATCH}z"
    trashed = ops.trash(REL, batch)
    # the worker restarts and recovers while the web process is inside its restore
    monkeypatch.setattr(fileops, "_hook", lambda step: ops.recover() if step == "restore" else None)
    ops.restore(trashed, REL, batch)
    monkeypatch.setattr(fileops, "_hook", lambda _step: None)
    assert ops.has(REL) and journal.pending() == []

    assert tag_undo.unfinished(sessions, ops) == []
    (data_dir / "library" / REL).unlink()  # later the track moves off REL
    assert tag_undo.unfinished(sessions, ops) == []
    tag_undo.recover(sessions, ops)
    assert not ops.has(REL)  # nothing resurrected


def test_recover_survives_a_purged_trash_file(
    ops: FileOps,
    make_src: MakeSrc,
    sessions: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    ops.import_file(make_src(), REL, lambda p, _op: None, "import")
    trashed = ops.trash(REL, f"{tag_undo.BATCH}x")
    (ops.trash_dir / trashed).unlink()
    assert len(tag_undo.unfinished(sessions, ops)) == 1

    with caplog.at_level(logging.WARNING):
        tag_undo.recover(sessions, ops)  # must not raise
    assert "is gone" in caplog.text
