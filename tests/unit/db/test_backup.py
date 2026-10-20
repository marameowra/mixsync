import asyncio
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

import mixsync.db.migrate
from mixsync.__main__ import main
from mixsync.core.config import Settings
from mixsync.core.jobs import JobKind
from mixsync.db.backup import KEEP, NightlyBackup, backup
from mixsync.db.engine import make_engine
from mixsync.db.migrate import upgrade
from mixsync.db.models.work import Job
from mixsync.db.queue import JobRecord

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
OLD = "0018_request_lifecycle"
_CFG = Config()
_CFG.set_main_option(
    "script_location", str(Path(mixsync.db.migrate.__file__).parent / "migrations")
)
HEAD = ScriptDirectory.from_config(_CFG).get_current_head()
assert HEAD
assert OLD != HEAD  # OLD must stay behind head


class Clock:
    def now(self) -> datetime:
        return NOW


@pytest.fixture
def bdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "backups"
    monkeypatch.setenv("MIXSYNC_BACKUP_DIR", str(d))
    return d


def _pg(settings: Settings) -> bool:
    return settings.database_url.startswith("postgresql")


def _files(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def _version(settings: Settings) -> str | None:
    with make_engine(settings).connect() as c:
        return c.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _readonly(d: Path) -> None:
    d.mkdir(exist_ok=True)
    d.chmod(stat.S_IRUSR | stat.S_IXUSR)  # the engine cannot create a file in it


def _job(i: int) -> JobRecord:
    return JobRecord(
        id=i,
        kind=JobKind.DB_BACKUP,
        payload={},
        status="running",  # type: ignore[arg-type]
        attempts=1,
        max_attempts=5,
        locked_by=None,
        run_after=NOW,
        lease_expires_at=None,
        last_error=None,
    )


def test_backup_command_writes_a_copy_with_the_same_rows(
    settings: Settings, bdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    upgrade()
    code = main(["backup"])
    out = capsys.readouterr()
    if _pg(settings):
        assert code != 0
        assert "pg_dump" in out.err
        assert _files(bdir) == []
        return
    assert code == 0
    (path,) = bdir.glob("mixsync-*.db")
    assert out.out.strip() == str(path)
    src = sqlite3.connect(settings.database_url.removeprefix("sqlite:///"))
    dst = sqlite3.connect(path)
    q = "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    tables = [r[0] for r in src.execute(q)]
    assert tables == [r[0] for r in dst.execute(q)]
    for t in tables:
        sql = f'SELECT count(*) FROM "{t}"'
        assert src.execute(sql).fetchone() == dst.execute(sql).fetchone()


def test_backup_keeps_the_seven_newest_and_other_files(settings: Settings, tmp_path: Path) -> None:
    if _pg(settings):
        return
    upgrade()
    d = tmp_path / "b"
    d.mkdir()
    for day in range(1, 10):
        (d / f"mixsync-2026-09-{day:02}.db").write_text("old")
    (d / "notes.txt").write_text("mine")
    backup(make_engine(settings), d, Clock())
    backup(make_engine(settings), d, Clock())  # same day: replaces, no extra file
    names = _files(d)
    assert [n for n in names if n.startswith("mixsync-")] == [
        *(f"mixsync-2026-09-{day:02}.db" for day in range(4, 10)),
        "mixsync-2026-10-11.db",
    ]
    assert len(names) == KEEP + 1
    assert "notes.txt" in names


def test_a_failed_backup_leaves_nothing_and_prunes_nothing(
    settings: Settings, tmp_path: Path
) -> None:
    if _pg(settings):
        return
    upgrade()
    d = tmp_path / "b"
    d.mkdir()
    for day in range(1, 10):
        (d / f"mixsync-2026-09-{day:02}.db").write_text("old")
    d.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(Exception, match=r".+"):
            backup(make_engine(settings), d, Clock())
    finally:
        d.chmod(stat.S_IRWXU)
    assert len(_files(d)) == 9
    assert "mixsync-2026-10-11.db" not in _files(d)


def test_migrate_backs_up_only_a_database_that_is_behind(settings: Settings, bdir: Path) -> None:
    upgrade(OLD)  # fresh: nothing to protect
    assert _files(bdir) == []
    upgrade()  # behind head
    assert len(_files(bdir)) == (0 if _pg(settings) else 1)
    assert _version(settings) == HEAD
    before = _files(bdir)
    upgrade()  # at head
    assert _files(bdir) == before


def test_a_fresh_database_upgraded_to_head_is_not_backed_up(settings: Settings, bdir: Path) -> None:
    upgrade()
    assert _files(bdir) == []


def test_a_failed_backup_stops_the_upgrade(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if _pg(settings):
        return
    upgrade(OLD)
    d = tmp_path / "ro"
    monkeypatch.setenv("MIXSYNC_BACKUP_DIR", str(d))
    _readonly(d)
    try:
        with pytest.raises(Exception, match=r".+"):
            upgrade()
    finally:
        d.chmod(stat.S_IRWXU)
    assert _version(settings) == OLD


def test_nightly_runs_the_backup_and_chains_once(
    settings: Settings, sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    d = tmp_path / "b"
    job = NightlyBackup(sessions, Clock(), make_engine(settings), d)
    job.start_chains()
    job.start_chains()  # a live chain exists
    asyncio.run(job.run(_job(1)))
    asyncio.run(job.run(_job(1)))  # a retry adds no second continuation
    assert _files(d) == ([] if _pg(settings) else ["mixsync-2026-10-11.db"])
    with sessions() as s:
        jobs = list(s.scalars(select(Job).where(Job.kind == JobKind.DB_BACKUP).order_by(Job.id)))
    assert len(jobs) == 2  # the first, and tomorrow's (also on Postgres: the chain goes on)
    assert jobs[1].run_after.replace(tzinfo=UTC) > NOW


def test_a_failing_nightly_still_chains(
    settings: Settings, sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    if _pg(settings):
        return
    d = tmp_path / "b"
    _readonly(d)
    try:
        with pytest.raises(Exception, match=r".+"):
            asyncio.run(NightlyBackup(sessions, Clock(), make_engine(settings), d).run(_job(1)))
    finally:
        d.chmod(stat.S_IRWXU)
    with sessions() as s:
        assert len(list(s.scalars(select(Job).where(Job.kind == JobKind.DB_BACKUP)))) == 1
