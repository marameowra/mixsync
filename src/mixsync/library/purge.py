"""The daily trash purge: a self-rescheduling job chain around FileOps.purge_trash."""

import random
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.pipeline import Handler
from mixsync.core.clock import Clock
from mixsync.core.jobs import JobKind, JobState
from mixsync.db.models.library import Plan
from mixsync.db.models.safety import FileOp
from mixsync.db.models.work import Job
from mixsync.db.queue import JobRecord, enqueue_in
from mixsync.library.fileops import FileOps

INTERVAL = timedelta(days=1)
LIVE = (JobState.QUEUED, JobState.LEASED, JobState.RUNNING, JobState.RETRY_WAIT)
UNDOABLE = ("running", "done", "failed", "undoing")  # undo restores these plans' files from trash
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
        """Trash paths an undoable plan's undo would restore."""
        with self.sessions() as s:
            batches = select(Plan.batch_id).where(Plan.status.in_(UNDOABLE), Plan.batch_id != "")
            return set(
                s.scalars(
                    select(FileOp.dst).where(FileOp.kind == "trash", FileOp.batch_id.in_(batches))
                )
            )

    async def purge(self, job: JobRecord) -> None:
        try:
            self.fileops.purge_trash(
                self.days, f"purge-{self.clock.now().date().isoformat()}", self.protected()
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
