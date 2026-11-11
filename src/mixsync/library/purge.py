"""The daily trash purge: a self-rescheduling job chain around FileOps.purge_trash."""

import asyncio
import random
from collections.abc import Callable, Collection
from datetime import timedelta
from typing import Any, NamedTuple

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.pipeline import Handler
from mixsync.core.clock import Clock
from mixsync.core.jobs import LIVE, JobKind, JobState
from mixsync.db import progress
from mixsync.db.base import Base
from mixsync.db.models.infra import HttpCache
from mixsync.db.models.library import Plan
from mixsync.db.models.safety import FileOp
from mixsync.db.models.work import Job
from mixsync.db.queue import JobRecord, enqueue_in
from mixsync.db.undo import UNDOABLE_STATUSES, undoable
from mixsync.library import tag_undo
from mixsync.library.fileops import FileOps

BATCH = 500  # rows per transaction: each holds SQLite's write lock only briefly (#422)
INTERVAL = timedelta(days=1)
MONITOR_KEEP = timedelta(days=7)  # finished download-monitor rows; nothing reads them again
Rng = Callable[[float, float], float]


class TrashFolder(NamedTuple):
    day: str
    files: int
    size: int
    protected: bool  # holds trash an undo or a half-done op needs; never purged early


RECENT = timedelta(days=1)  # today and yesterday: an in-flight move may trash into them unjournaled


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
                for op, done in zip(p.ops, progress.done_flags(s, p), strict=True):
                    if op["op"] in ("move", "trash") and not done:
                        keep.update(
                            s.scalars(
                                select(FileOp.dst).where(
                                    FileOp.kind == "trash",
                                    FileOp.batch_id == p.batch_id,
                                    FileOp.src == op["old"]["path"],
                                )
                            )
                        )
            keep.update(op.dst for op in tag_undo.unfinished(self.sessions, self.fileops))
            return keep

    def folders(self) -> list[TrashFolder]:
        """Each trash day folder, newest first, with its usage and whether it is protected."""
        held = self._held(self.protected())
        usage = self.fileops.trash_usage()
        return [TrashFolder(d, *usage[d], d in held) for d in sorted(usage, reverse=True)]

    def purge_early(self, days: Collection[str]) -> tuple[int, list[str]]:
        """Purge the chosen day folders now, whatever their age, journaled like the daily purge.
        Protection is checked here, at execution: a folder holding any protected path is
        skipped whole. Returns (files deleted, skipped folders)."""
        keep = self.protected()
        skipped = sorted(set(days) & self._held(keep))
        chosen = set(days) - set(skipped)
        batch = f"purge-early-{self.clock.now().isoformat()}"
        return self.fileops.purge_trash(0, batch, keep, only=chosen), skipped

    def _held(self, keep: set[str]) -> set[str]:
        """Day folders an early purge must skip: those holding protected paths, plus any day
        from yesterday on, which a running merge or tag restore may be trashing into right now."""
        floor = (self.clock.now().date() - RECENT).isoformat()  # ISO dates sort as strings
        days = self.fileops.trash_usage()
        return {p.split("/", 1)[0] for p in keep} | {d for d in days if d >= floor}

    def _purge_monitor_rows(self) -> None:
        """One row per 30 s per transfer; only their dedup key names them."""
        self._delete_in_batches(
            Job,
            Job.id,
            Job.kind == JobKind.DOWNLOAD,
            Job.status == JobState.SUCCEEDED,
            Job.idempotency_key.like("monitor:%"),
            Job.updated_at < self.clock.now() - MONITOR_KEEP,
        )

    def _purge_cache_rows(self) -> None:
        """Expired cache rows are dead weight."""
        self._delete_in_batches(HttpCache, HttpCache.key, HttpCache.expires_at < self.clock.now())

    def _delete_in_batches(self, model: type[Base], pk: Any, *where: Any) -> None:
        """Each batch in its own transaction, so the first purge after a long gap never holds
        the write lock long."""
        old = select(pk).where(*where).limit(BATCH)
        while True:
            with self.sessions.begin() as s:
                if not s.execute(delete(model).where(pk.in_(old)).returning(pk)).all():
                    return

    async def purge(self, job: JobRecord) -> None:
        try:
            await asyncio.to_thread(
                self.fileops.purge_trash,
                self.days,
                f"purge-{self.clock.now().date().isoformat()}",
                self.protected(),
            )
            await asyncio.to_thread(
                self.fileops.purge_quarantine,
                self.days,
                f"purge-quarantine-{self.clock.now().date().isoformat()}",
            )
            # after the file purge, so a failing cache delete cannot skip it
            await asyncio.to_thread(self._purge_cache_rows)
            await asyncio.to_thread(self._purge_monitor_rows)
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
