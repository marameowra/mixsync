"""The daily trash purge: a self-rescheduling job chain around FileOps.purge_trash."""

import asyncio
import random
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.pipeline import Handler
from mixsync.core.clock import Clock
from mixsync.core.jobs import JobKind, JobState
from mixsync.db.models.infra import HttpCache
from mixsync.db.models.library import Plan
from mixsync.db.models.safety import FileOp
from mixsync.db.models.work import Job
from mixsync.db.queue import JobRecord, enqueue_in
from mixsync.db.undo import UNDOABLE_STATUSES, undoable
from mixsync.library.fileops import FileOps

INTERVAL = timedelta(days=1)
LIVE = (JobState.QUEUED, JobState.LEASED, JobState.RUNNING, JobState.RETRY_WAIT)
Rng = Callable[[float, float], float]


class TrashPurger:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        clock: Clock,
        fileops: FileOps,
        days: int,
        rng: Rng = random.uniform,
    ) -> None:
        self.sessions, self.clock, self.fileops, self.days, self.rng = (
            sessions,
            clock,
            fileops,
            days,
            rng,
        )

    def handlers(self) -> dict[JobKind, Handler]:
        return {JobKind.TRASH_PURGE: self.purge}

    def start_chains(self) -> None:
        """On startup: queue a purge unless a chain is already live."""
        with self.sessions.begin() as s:
            live = s.scalar(
                select(Job.id).where(Job.kind == JobKind.TRASH_PURGE, Job.status.in_(LIVE)).limit(1)
            )
            if live is None:
                enqueue_in(s, self.clock.now(), JobKind.TRASH_PURGE, {})

    def protected(self) -> set[str]:
        """Trash paths a plan that can still be undone, or a half-done op, would need."""
        now = self.clock.now()
        with self.sessions() as s:
            plans = s.scalars(
                select(Plan).where(Plan.status.in_(UNDOABLE_STATUSES), Plan.batch_id != "")
            )
            # +1 day margin: a snapshot must never call a plan expired while undo may accept it.
            batches = [p.batch_id for p in plans if undoable(p, now, self.days + 1)]
            keep = set(
                s.scalars(
                    select(FileOp.dst).where(FileOp.kind == "trash", FileOp.batch_id.in_(batches))
                )
            )
            # Second layer, whatever the plan's status: a trashed original whose op is not
            # done (a half-finished move) may be the only copy.
            live = select(Plan).where(
                Plan.status.not_in(("undone", "discarded", "draft")), Plan.batch_id != ""
            )
            for p in s.scalars(live):
                for op in p.ops:
                    if op["op"] in ("move", "trash") and not op["done"]:
                        keep.update(
                            s.scalars(
                                select(FileOp.dst).where(
                                    FileOp.kind == "trash",
                                    FileOp.batch_id == p.batch_id,
                                    FileOp.src == op["old"]["path"],
                                )
                            )
                        )
            return keep

    async def purge(self, job: JobRecord) -> None:
        try:
            with self.sessions.begin() as s:  # expired cache rows are dead weight (bodies stay)
                s.execute(delete(HttpCache).where(HttpCache.expires_at < self.clock.now()))
            await asyncio.to_thread(
                self.fileops.purge_trash,
                self.days,
                f"purge-{self.clock.now().date().isoformat()}",
                self.protected(),
            )
        finally:  # the chain goes on after a failure too; the key makes a retry add no second
            now = self.clock.now()
            with self.sessions.begin() as s:
                enqueue_in(
                    s,
                    now,
                    JobKind.TRASH_PURGE,
                    {},
                    run_after=now + INTERVAL * self.rng(0.75, 1.25),
                    idempotency_key=f"trash-purge:after:{job.id}",
                )
