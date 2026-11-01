import logging
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from mixsync import worker
from mixsync.core.clock import SystemClock
from mixsync.core.config import Settings
from mixsync.db import engine as db_engine
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.journal import SqlJournal
from mixsync.db.migrate import upgrade
from mixsync.db.models.safety import FileOp
from mixsync.library.fileops import FileOps
from mixsync.wiring import build_services


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A file-backed SQLite DB with a short busy_timeout, so a held lock surfaces quickly."""
    path = tmp_path / "lock.db"
    monkeypatch.setenv("MIXSYNC_DATABASE_URL", f"sqlite:///{path}")
    pragmas = tuple(p.replace("5000", "50") for p in db_engine._PRAGMAS)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(db_engine, "_PRAGMAS", pragmas)
    upgrade()
    return path


def _setup(tmp_path: Path) -> tuple[FileOps, sessionmaker[Session]]:
    sessions = make_session_factory(make_engine(Settings()))
    data = tmp_path / "data"
    (data / "library").mkdir(parents=True)
    journal = SqlJournal(sessions, SystemClock())
    journal.begin("b", "copy", str(tmp_path / "src.flac"), "A/x.flac")
    return FileOps(data, tmp_path / "downloads", journal, SystemClock()), sessions


def _statuses(sessions: sessionmaker[Session]) -> list[str]:
    with sessions() as s:
        return [o.status for o in s.scalars(select(FileOp))]


def _hold_lock(db: Path, seconds: float | None) -> sqlite3.Connection:
    c = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
    c.execute("BEGIN IMMEDIATE")
    if seconds is not None:
        threading.Timer(seconds, c.close).start()
    return c


def test_recovery_retries_until_the_lock_is_released(db: Path, tmp_path: Path) -> None:
    fileops, sessions = _setup(tmp_path)
    _hold_lock(db, 0.5)
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        time.sleep(0.2)

    worker._recover(fileops, sessions, sleep)  # pyright: ignore[reportPrivateUsage]

    assert sleeps  # it did have to retry
    assert _statuses(sessions) == ["rolled_back"]


def test_recovery_gives_up_with_one_error_line(
    db: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fileops, sessions = _setup(tmp_path)
    lock = _hold_lock(db, None)
    sleeps: list[float] = []
    try:
        with caplog.at_level(logging.WARNING), pytest.raises(OperationalError):
            worker._recover(fileops, sessions, sleeps.append)  # pyright: ignore[reportPrivateUsage]
    finally:
        lock.close()

    assert len(sleeps) == worker.RECOVERY_ATTEMPTS - 1  # pyright: ignore[reportPrivateUsage]
    assert 100 <= sum(sleeps) <= 140  # about two minutes
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == ["startup recovery could not commit: database stayed locked"]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == len(sleeps)
    assert _statuses(sessions) == ["started"]  # still pending for the next start


def test_other_errors_are_not_retried(db: Path, tmp_path: Path) -> None:
    fileops, sessions = _setup(tmp_path)
    calls: list[int] = []

    def boom() -> None:
        calls.append(1)
        raise ValueError("boom")

    fileops.recover = boom  # type: ignore[method-assign]
    with pytest.raises(ValueError):
        worker._recover(fileops, sessions, lambda _s: None)  # pyright: ignore[reportPrivateUsage]
    assert calls == [1]


def test_no_queue_exists_before_recovery_completes(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fileops, sessions = _setup(tmp_path)
    services = build_services(Settings(), sessions)
    object.__setattr__(services, "fileops", fileops)
    seen: list[list[str]] = []

    def queue(*_a: object, **_k: object) -> None:
        seen.append(_statuses(sessions))
        raise StopIteration

    monkeypatch.setattr(worker, "JobQueue", queue)
    with pytest.raises(StopIteration):
        worker._new_queue(services)  # pyright: ignore[reportPrivateUsage]
    assert seen == [["rolled_back"]]
