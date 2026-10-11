"""Submitting fingerprints to AcoustID after a promote plan finishes.

ACOUSTID_SUBMIT (one per plan) fingerprints the promoted files at their new paths and sends one
batch; ACOUSTID_STATUS then polls the submission ids a few times with growing delays. AcoustID
processes submissions asynchronously and has no retract, so an undo does not withdraw them.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.pipeline import Handler
from mixsync.core.clock import Clock
from mixsync.core.jobs import JobKind
from mixsync.core.protocols import AcoustIdSubmit, Fingerprinter
from mixsync.core.types import Fingerprint
from mixsync.db.models.library import Plan
from mixsync.db.queue import JobRecord, enqueue_in
from mixsync.library.fileops import FileOps

log = logging.getLogger(__name__)

POLL_DELAYS = (timedelta(minutes=1), timedelta(minutes=10), timedelta(hours=1))


def enqueue_submit(s: Session, now: datetime, plan_id: int) -> None:
    enqueue_in(
        s, now, JobKind.ACOUSTID_SUBMIT, {"plan_id": plan_id}, idempotency_key=f"acoustid:{plan_id}"
    )


@dataclass(frozen=True)
class AcoustIdSubmitter:
    sessions: sessionmaker[Session]
    clock: Clock
    fileops: FileOps
    fingerprint: Fingerprinter
    acoustid: AcoustIdSubmit
    enabled: bool  # both the app key and the user key are set

    def handlers(self) -> dict[JobKind, Handler]:
        return {JobKind.ACOUSTID_SUBMIT: self.submit, JobKind.ACOUSTID_STATUS: self.poll}

    async def submit(self, job: JobRecord) -> None:
        plan_id = job.payload["plan_id"]
        if not self.enabled:
            log.info("plan %s: AcoustID keys are not set; not submitting fingerprints", plan_id)
            return
        with self.sessions() as s:
            ops = s.get_one(Plan, plan_id).ops
        items: list[tuple[Fingerprint, str]] = []
        for op in ops:
            if op["op"] != "move":
                continue
            try:
                fp = await self.fingerprint(self.fileops.library / op["new_path"])
            except Exception:
                log.warning("plan %s: %s not fingerprinted, skipped", plan_id, op["new_path"])
                continue
            items.append((fp, op["tags"]["recording_id"]))
        if not items:
            return
        ids = await self.acoustid.submit(items)
        log.info("plan %s: submitted %s fingerprints to AcoustID", plan_id, len(items))
        self._enqueue_poll(plan_id, ids, 0)

    def _enqueue_poll(self, plan_id: int, ids: list[int], attempt: int) -> None:
        now = self.clock.now()
        with self.sessions.begin() as s:
            enqueue_in(
                s,
                now,
                JobKind.ACOUSTID_STATUS,
                {"plan_id": plan_id, "ids": ids, "attempt": attempt},
                run_after=now + POLL_DELAYS[attempt],
                idempotency_key=f"acoustid-status:{plan_id}:{attempt}",
            )

    async def poll(self, job: JobRecord) -> None:
        plan_id, ids, attempt = job.payload["plan_id"], job.payload["ids"], job.payload["attempt"]
        status = await self.acoustid.submission_status(ids)
        if any(v == "pending" for v in status.values()) and attempt + 1 < len(POLL_DELAYS):
            self._enqueue_poll(plan_id, ids, attempt + 1)
            return
        log.info("plan %s: AcoustID submissions %s", plan_id, status)
