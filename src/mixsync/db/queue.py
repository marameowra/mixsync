from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import Connection, Engine, Row, Table, exists, select, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from mixsync.core.clock import Clock
from mixsync.core.jobs import DEFAULT_LEASE, JobKind, JobState, backoff, transition
from mixsync.db.models.work import Job, Request

_J = cast(Table, Job.__table__)
_R = cast(Table, Request.__table__)
_IN_PROGRESS = ("searching", "no_results", "downloading", "verifying", "importing")


class LeaseLostError(Exception):
    """The job is no longer leased to this worker."""


@dataclass(frozen=True)
class JobRecord:
    id: int
    kind: JobKind
    payload: dict[str, Any]
    status: JobState
    attempts: int
    max_attempts: int
    locked_by: str | None
    run_after: datetime
    lease_expires_at: datetime | None
    last_error: str | None


def _record(r: Row[Any]) -> JobRecord:
    return JobRecord(**{f.name: getattr(r, f.name) for f in fields(JobRecord)})


def _fail_request(c: Connection, job_id: int, error: str, now: datetime) -> None:
    """A pipeline job that failed for good must not leave its request in progress forever,
    unless a newer job for the request has taken over."""
    payload: dict[str, Any] = (
        c.execute(select(_J.c.payload).where(_J.c.id == job_id)).scalar() or {}
    )
    if "request_id" in payload:
        c.execute(
            update(_R)
            .where(
                _R.c.id == payload["request_id"],
                _R.c.status.in_(_IN_PROGRESS),
                ~exists().where(
                    _J.c.id > job_id,
                    _J.c.payload["request_id"].as_integer() == payload["request_id"],
                ),
            )
            .values(status="failed", last_error=error, updated_at=now)
        )


class JobQueue:
    def __init__(self, engine: Engine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    def get(self, job_id: int) -> JobRecord:
        with self._engine.connect() as c:
            return _record(c.execute(select(_J).where(_J.c.id == job_id)).one())

    def claim(self, worker: str, lease: timedelta = DEFAULT_LEASE) -> JobRecord | None:
        """Lease the next due queued job. Expired leases are returned to queued by reap()."""
        now = self._clock.now()
        pick = (
            select(_J.c.id)
            .where(_J.c.status == JobState.QUEUED, _J.c.run_after <= now)
            .order_by(_J.c.priority, _J.c.id)
            .limit(1)
            .with_for_update(skip_locked=True)  # Postgres only; ignored by SQLite
            .scalar_subquery()
        )
        with self._engine.connect() as c:
            if c.dialect.name == "sqlite":
                c.exec_driver_sql("BEGIN IMMEDIATE")  # take the write lock before selecting
            row = c.execute(
                update(_J)
                .where(_J.c.id == pick)
                .values(
                    status=transition(JobState.QUEUED, JobState.LEASED),
                    locked_by=worker,
                    lease_expires_at=now + lease,
                    attempts=_J.c.attempts + 1,
                    updated_at=now,
                )
                .returning(*_J.c)
            ).first()
            c.commit()
        return _record(row) if row else None

    def heartbeat(self, job_id: int, worker: str, lease: timedelta = DEFAULT_LEASE) -> bool:
        """Extend the lease. False means the lease was lost."""
        now = self._clock.now()
        with self._engine.begin() as c:
            res = c.execute(
                update(_J)
                .where(
                    _J.c.id == job_id,
                    _J.c.locked_by == worker,
                    _J.c.status.in_([JobState.LEASED, JobState.RUNNING]),
                )
                .values(lease_expires_at=now + lease, updated_at=now)
            )
        return res.rowcount == 1

    def start(self, job_id: int, worker: str) -> None:
        self._move(job_id, worker, JobState.RUNNING, keep_lease=True)

    def complete(self, job_id: int, worker: str) -> None:
        self._move(job_id, worker, JobState.SUCCEEDED)

    def fail(self, job_id: int, worker: str, error: str) -> None:
        self._move(job_id, worker, JobState.FAILED, last_error=error)

    def retry(self, job_id: int, worker: str, error: str) -> JobState:
        """-> retry_wait with a backoff delay, or failed once attempts are exhausted."""
        try:
            job = self.get(job_id)
        except NoResultFound:  # the row was deleted under us
            raise LeaseLostError(f"job {job_id} is gone") from None
        if job.attempts >= job.max_attempts:
            self.fail(job_id, worker, error)
            return JobState.FAILED
        run_after = self._clock.now() + backoff(job.attempts)
        self._move(job_id, worker, JobState.RETRY_WAIT, last_error=error, run_after=run_after)
        return JobState.RETRY_WAIT

    def reap(self) -> int:
        """Expired leases and due retry_wait jobs go back to queued. Returns the count."""
        now = self._clock.now()
        n = 0
        with self._engine.begin() as c:
            # A job that keeps killing its worker must not be requeued forever.
            dead = (
                _J.c.status == JobState.RUNNING,
                _J.c.lease_expires_at < now,
                _J.c.attempts >= _J.c.max_attempts,
            )
            for (job_id,) in c.execute(select(_J.c.id).where(*dead)).all():
                _fail_request(c, job_id, "lease expired with no attempts left", now)
            n += c.execute(
                update(_J)
                .where(*dead)
                .values(
                    status=transition(JobState.RUNNING, JobState.FAILED),
                    locked_by=None,
                    lease_expires_at=None,
                    last_error="lease expired with no attempts left",
                    updated_at=now,
                )
            ).rowcount
            for src, due in (
                (JobState.LEASED, _J.c.lease_expires_at < now),
                (JobState.RUNNING, _J.c.lease_expires_at < now),
                (JobState.RETRY_WAIT, _J.c.run_after <= now),
            ):
                n += c.execute(
                    update(_J)
                    .where(_J.c.status == src, due)
                    .values(
                        status=transition(src, JobState.QUEUED),
                        locked_by=None,
                        lease_expires_at=None,
                        updated_at=now,
                    )
                ).rowcount
        return n

    def _move(
        self, job_id: int, worker: str, new: JobState, keep_lease: bool = False, **values: Any
    ) -> None:
        now = self._clock.now()
        with self._engine.begin() as c:
            cur = self._status(c, job_id, worker)
            if not keep_lease:
                values |= {"locked_by": None, "lease_expires_at": None}
            res = c.execute(
                update(_J)
                .where(_J.c.id == job_id, _J.c.status == cur, _J.c.locked_by == worker)
                .values(status=transition(cur, new), updated_at=now, **values)
            )
            if res.rowcount != 1:
                raise LeaseLostError(f"job {job_id} is no longer held by {worker}")
            if new is JobState.FAILED:
                _fail_request(c, job_id, values["last_error"], now)

    @staticmethod
    def _status(c: Connection, job_id: int, worker: str) -> JobState:
        status = c.execute(
            select(_J.c.status).where(_J.c.id == job_id, _J.c.locked_by == worker)
        ).scalar()
        if status is None:
            raise LeaseLostError(f"job {job_id} is no longer held by {worker}")
        return status


USER_PRIORITY = -10  # a job a UI click queued runs before any background work


def enqueue_in(
    s: Session,
    now: datetime,
    kind: JobKind,
    payload: dict[str, Any],
    *,
    run_after: datetime | None = None,
    max_attempts: int = 5,
    priority: int = 0,  # lower runs first
    idempotency_key: str | None = None,
) -> None:
    """Add a job inside the caller's transaction, so it commits (or not) with the caller's own
    changes. With an existing idempotency_key, adds nothing."""
    if idempotency_key and s.scalar(select(Job.id).where(Job.idempotency_key == idempotency_key)):
        return
    # a web request's session (db.engine.user_sessions); an explicit priority, like a
    # migration's bulk VERIFY jobs, is kept so a big confirm can't jump the queue
    if s.info.get("user") and priority == 0:
        priority = USER_PRIORITY
    s.add(
        Job(
            kind=kind,
            payload=payload,
            status=JobState.QUEUED,
            priority=priority,
            attempts=0,
            max_attempts=max_attempts,
            run_after=run_after or now,
            idempotency_key=idempotency_key,
        )
    )
    s.flush()
