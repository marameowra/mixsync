import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from mixsync.core.config import Settings
from mixsync.core.errors import PermanentError, TransientError
from mixsync.core.jobs import JobKind, JobState, backoff
from mixsync.db.engine import make_engine
from mixsync.db.migrate import upgrade
from mixsync.db.models.auth import User
from mixsync.db.models.work import Job, Request
from mixsync.db.queue import JobQueue, JobRecord, LeaseLostError
from mixsync.worker import HANDLERS, Handler, run_job

LEASE = timedelta(seconds=30)


class FakeClock:
    def __init__(self) -> None:
        self.t = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.t

    def advance(self, d: timedelta) -> None:
        self.t += d


@pytest.fixture
def engine(settings: Settings) -> Engine:
    upgrade()
    return make_engine(settings)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def q(engine: Engine, clock: FakeClock) -> JobQueue:
    return JobQueue(engine, clock)


def test_idempotency_key_blocks_duplicate(q: JobQueue) -> None:
    a = q.enqueue(JobKind.NOOP, idempotency_key="k")
    b = q.enqueue(JobKind.NOOP, idempotency_key="k")
    assert a.id == b.id
    assert q.enqueue(JobKind.NOOP).id != a.id


def test_claim_race_has_one_winner(engine: Engine, clock: FakeClock) -> None:
    q1, q2 = JobQueue(engine, clock), JobQueue(make_engine(), clock)  # two connections, file DB
    q1.enqueue(JobKind.NOOP)
    barrier = threading.Barrier(2)
    won: list[JobRecord | None] = []

    def go(q: JobQueue, w: str) -> None:
        barrier.wait()
        won.append(q.claim(w, LEASE))

    ts = [threading.Thread(target=go, args=(q, w)) for q, w in ((q1, "a"), (q2, "b"))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(j is not None for j in won) == 1


def test_claim_respects_run_after_and_priority(q: JobQueue, clock: FakeClock) -> None:
    q.enqueue(JobKind.NOOP, run_after=clock.now() + timedelta(hours=1))
    low = q.enqueue(JobKind.NOOP, priority=5)
    high = q.enqueue(JobKind.NOOP, priority=1)
    first, second = q.claim("w", LEASE), q.claim("w", LEASE)
    assert first and second and (first.id, second.id) == (high.id, low.id)
    assert q.claim("w", LEASE) is None


def test_expired_lease_is_reclaimed(q: JobQueue, clock: FakeClock) -> None:
    j = q.enqueue(JobKind.NOOP)
    q.claim("w1", LEASE)
    assert q.reap() == 0
    clock.advance(LEASE + timedelta(seconds=1))
    assert q.reap() == 1
    assert q.get(j.id).status is JobState.QUEUED
    assert q.claim("w2", LEASE) is not None


def test_heartbeat_extends_lease(q: JobQueue, clock: FakeClock) -> None:
    j = q.enqueue(JobKind.NOOP)
    q.claim("w1", LEASE)
    clock.advance(timedelta(seconds=20))
    assert q.heartbeat(j.id, "w1", LEASE)
    clock.advance(timedelta(seconds=20))
    assert q.reap() == 0
    assert not q.heartbeat(j.id, "w2", LEASE)


def test_phase1_exit_dead_worker_job_completed_by_another(q: JobQueue, clock: FakeClock) -> None:
    j = q.enqueue(JobKind.NOOP)
    q.claim("w1", LEASE)
    q.start(j.id, "w1")  # w1 dies here
    clock.advance(LEASE + timedelta(seconds=1))
    q.reap()
    job = q.claim("w2", LEASE)
    assert job is not None and job.id == j.id
    asyncio.run(run_job(q, job, "w2", HANDLERS, LEASE))
    assert q.get(j.id).status is JobState.SUCCEEDED
    with pytest.raises(LeaseLostError):  # w1 waking up late cannot finish it
        q.complete(j.id, "w1")


def _run(q: JobQueue, exc: Exception) -> JobRecord:
    async def boom(_j: JobRecord) -> None:
        raise exc

    handlers: dict[JobKind, Handler] = {JobKind.NOOP: boom}
    job = q.claim("w", LEASE)
    assert job is not None
    asyncio.run(run_job(q, job, "w", handlers, LEASE))
    return q.get(job.id)


def test_transient_error_retries_then_requeues(q: JobQueue, clock: FakeClock) -> None:
    q.enqueue(JobKind.NOOP)
    j = _run(q, TransientError("later"))
    assert j.status is JobState.RETRY_WAIT and j.last_error == "later"
    assert q.reap() == 0
    clock.advance(backoff(1) + timedelta(seconds=1))
    assert q.reap() == 1
    assert q.get(j.id).status is JobState.QUEUED


def test_transient_error_fails_when_attempts_exhausted(q: JobQueue) -> None:
    q.enqueue(JobKind.NOOP, max_attempts=1)
    assert _run(q, TransientError("x")).status is JobState.FAILED


def test_permanent_error_fails(q: JobQueue) -> None:
    q.enqueue(JobKind.NOOP)
    assert _run(q, PermanentError("no")).status is JobState.FAILED


@pytest.mark.parametrize("exc", [TransientError("x"), RuntimeError("bug")])
def test_handler_error_after_job_deleted_does_not_raise(
    q: JobQueue, engine: Engine, exc: Exception
) -> None:
    q.enqueue(JobKind.NOOP)

    async def delete_then_boom(j: JobRecord) -> None:
        with Session(engine) as s, s.begin():
            s.delete(s.get(Job, j.id))
        raise exc

    job = q.claim("w", LEASE)
    assert job is not None
    asyncio.run(run_job(q, job, "w", {JobKind.NOOP: delete_then_boom}, LEASE))  # no LeaseLostError


def test_job_that_keeps_killing_its_worker_fails(q: JobQueue, clock: FakeClock) -> None:
    j = q.enqueue(JobKind.NOOP, max_attempts=1)
    q.claim("w1", LEASE)
    q.start(j.id, "w1")  # w1 dies here, and no attempts are left
    clock.advance(LEASE + timedelta(seconds=1))
    assert q.reap() == 1
    assert q.get(j.id).status is JobState.FAILED


def _request(engine: Engine, status: str) -> int:
    with Session(engine) as s, s.begin():
        s.add(User(username="u", password_hash="x", status="active"))
        s.flush()
        req = Request(user_id=s.query(User).one().id, release_mbid="m", status=status)
        s.add(req)
        s.flush()
        return req.id


def _status(engine: Engine, req_id: int) -> tuple[str, str | None]:
    with Session(engine) as s:
        req = s.get(Request, req_id)
        assert req
        return req.status, req.last_error


def test_permanent_failure_fails_the_request(q: JobQueue, engine: Engine) -> None:
    rid = _request(engine, "searching")
    q.enqueue(JobKind.NOOP, {"request_id": rid})
    _run(q, PermanentError("404"))
    assert _status(engine, rid) == ("failed", "PermanentError('404')")


def test_failure_leaves_a_request_that_moved_on(q: JobQueue, engine: Engine) -> None:
    rid = _request(engine, "imported")
    q.enqueue(JobKind.NOOP, {"request_id": rid})
    _run(q, PermanentError("stale"))
    assert _status(engine, rid) == ("imported", None)


def test_reaped_dead_job_fails_the_request(q: JobQueue, engine: Engine, clock: FakeClock) -> None:
    rid = _request(engine, "verifying")
    j = q.enqueue(JobKind.NOOP, {"request_id": rid}, max_attempts=1)
    q.claim("w1", LEASE)
    q.start(j.id, "w1")
    clock.advance(LEASE + timedelta(seconds=1))
    q.reap()
    assert _status(engine, rid)[0] == "failed"


def test_permanent_failure_fails_a_no_results_request(q: JobQueue, engine: Engine) -> None:
    rid = _request(engine, "no_results")
    q.enqueue(JobKind.NOOP, {"request_id": rid})
    _run(q, PermanentError("gone"))
    assert _status(engine, rid)[0] == "failed"


def test_failure_of_a_superseded_job_leaves_the_request(q: JobQueue, engine: Engine) -> None:
    rid = _request(engine, "no_results")
    q.enqueue(JobKind.NOOP, {"request_id": rid})
    q.enqueue(JobKind.NOOP, {"request_id": rid})  # the newer job now drives the request
    _run(q, PermanentError("stale"))  # fails the older job
    assert _status(engine, rid) == ("no_results", None)


def test_reaped_superseded_job_leaves_the_request(
    q: JobQueue, engine: Engine, clock: FakeClock
) -> None:
    rid = _request(engine, "no_results")
    j = q.enqueue(JobKind.NOOP, {"request_id": rid}, max_attempts=1)
    q.enqueue(JobKind.NOOP, {"request_id": rid})
    q.claim("w1", LEASE)
    q.start(j.id, "w1")
    clock.advance(LEASE + timedelta(seconds=1))
    q.reap()
    assert q.get(j.id).status is JobState.FAILED
    assert _status(engine, rid) == ("no_results", None)
