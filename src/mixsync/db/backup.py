"""SQLite backups: `VACUUM INTO` a dated file, keep the newest few, plus the nightly job chain."""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import Clock
from mixsync.core.jobs import LIVE, JobKind
from mixsync.db.models.work import Job
from mixsync.db.queue import JobRecord, enqueue_in

log = logging.getLogger(__name__)
KEEP = 7
NIGHTLY_AT = time(3)  # UTC
PG_MESSAGE = "backups are SQLite only; on Postgres use pg_dump"


def backup(engine: Engine, backup_dir: Path, clock: Clock) -> Path:
    """Write backup_dir/mixsync-<UTC date>.db and prune to the newest KEEP. The file appears
    only when complete; on failure nothing is pruned."""
    if engine.dialect.name != "sqlite":
        raise RuntimeError(PG_MESSAGE)
    backup_dir.mkdir(parents=True, exist_ok=True)
    final = backup_dir / f"mixsync-{clock.now().date().isoformat()}.db"
    tmp = backup_dir / f".{final.name}.tmp"  # not matched by the mixsync-*.db prune glob
    tmp.unlink(missing_ok=True)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.exec_driver_sql("VACUUM INTO ?", (str(tmp),))
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    for old in sorted(backup_dir.glob("mixsync-*.db"), reverse=True)[KEEP:]:
        old.unlink()
    return final


class NightlyBackup:
    def __init__(
        self, sessions: sessionmaker[Session], clock: Clock, engine: Engine, backup_dir: Path
    ) -> None:
        self.sessions, self.clock, self.engine, self.backup_dir = (
            sessions,
            clock,
            engine,
            backup_dir,
        )

    def handlers(self) -> dict[JobKind, Callable[[JobRecord], Awaitable[None]]]:
        return {JobKind.DB_BACKUP: self.run}

    def start_chains(self) -> None:
        """On startup: queue a backup unless a chain is already live."""
        with self.sessions.begin() as s:
            live = s.scalar(
                select(Job.id).where(Job.kind == JobKind.DB_BACKUP, Job.status.in_(LIVE)).limit(1)
            )
            if live is None:
                enqueue_in(s, self.clock.now(), JobKind.DB_BACKUP, {})

    async def run(self, job: JobRecord) -> None:
        try:
            if self.engine.dialect.name == "sqlite":
                path = await asyncio.to_thread(backup, self.engine, self.backup_dir, self.clock)
                log.info("database backup written: %s", path)
        finally:  # the chain goes on after a failure too; the key makes a retry add no second
            now = self.clock.now()
            nxt: datetime = datetime.combine(
                now.date() + timedelta(days=1), NIGHTLY_AT, tzinfo=now.tzinfo
            )
            with self.sessions.begin() as s:
                enqueue_in(
                    s,
                    now,
                    JobKind.DB_BACKUP,
                    {},
                    run_after=nxt,
                    idempotency_key=f"db-backup:{nxt.date().isoformat()}",
                )
