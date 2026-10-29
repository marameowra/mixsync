import asyncio
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from mixsync import worker
from mixsync.core.config import Settings
from mixsync.core.errors import PermanentError, TransientError
from mixsync.core.jobs import JobKind, JobState, backoff
from mixsync.db.engine import make_engine, make_session_factory, user_sessions
from mixsync.db.migrate import upgrade
from mixsync.db.models.work import Job
from mixsync.db.queue import USER_PRIORITY, JobQueue, JobRecord, LeaseLostError, enqueue_in
from mixsync.worker import HANDLERS, Handler, _heartbeat, run_job  # pyright: ignore[reportPrivateUsage]

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


def _ignore(*_: Any) -> None:
    """A final-failure hook for tests that do not care about it."""


@pytest.fixture
def q(engine: Engine, clock: FakeClock) -> JobQueue:
    return JobQueue(engine, clock, _ignore)


def enq(q: JobQueue, kind: JobKind, payload: dict[str, Any] | None = None, **kw: Any) -> JobRecord:
    """Enqueue through the production enqueue_in and return the job (newest or keyed)."""
    engine, clock = q._engine, q._clock  # pyright: ignore[reportPrivateUsage]
    with Session(engine) as s, s.begin():
        enqueue_in(s, clock.now(), kind, payload or {}, **kw)
    with Session(engine) as s:
        key = kw.get("idempotency_key")
        stmt = (
            select(Job.id).where(Job.idempotency_key == key)
            if key
            else select(Job.id).order_by(Job.id.desc())
        )
        return q.get(s.scalars(stmt).first() or 0)


def test_idempotency_key_blocks_duplicate(q: JobQueue) -> None:
    a = enq(q, JobKind.NOOP, idempotency_key="k")
    b = enq(q, JobKind.NOOP, idempotency_key="k")
    assert a.id == b.id
    assert enq(q, JobKind.NOOP).id != a.id
    with Session(q._engine) as s:  # pyright: ignore[reportPrivateUsage]
        assert len(s.scalars(select(Job).where(Job.idempotency_key == "k")).all()) == 1


def test_claim_race_has_one_winner(engine: Engine, clock: FakeClock) -> None:
    q1, q2 = (
        JobQueue(engine, clock, _ignore),
        JobQueue(make_engine(), clock, _ignore),
    )  # two connections, file DB
    enq(q1, JobKind.NOOP)
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
    enq(q, JobKind.NOOP, run_after=clock.now() + timedelta(hours=1))
    low = enq(q, JobKind.NOOP, priority=5)
    high = enq(q, JobKind.NOOP, priority=1)
    first, second = q.claim("w", LEASE), q.claim("w", LEASE)
    assert first and second and (first.id, second.id) == (high.id, low.id)
    assert q.claim("w", LEASE) is None


def test_expired_lease_is_reclaimed(q: JobQueue, clock: FakeClock) -> None:
    j = enq(q, JobKind.NOOP)
    q.claim("w1", LEASE)
    assert q.reap() == 0
    clock.advance(LEASE + timedelta(seconds=1))
    assert q.reap() == 1
    assert q.get(j.id).status is JobState.QUEUED
    assert q.claim("w2", LEASE) is not None


def test_heartbeat_extends_lease(q: JobQueue, clock: FakeClock) -> None:
    j = enq(q, JobKind.NOOP)
    q.claim("w1", LEASE)
    clock.advance(timedelta(seconds=20))
    assert q.heartbeat(j.id, "w1", LEASE)
    clock.advance(timedelta(seconds=20))
    assert q.reap() == 0
    assert not q.heartbeat(j.id, "w2", LEASE)


def test_phase1_exit_dead_worker_job_completed_by_another(q: JobQueue, clock: FakeClock) -> None:
    j = enq(q, JobKind.NOOP)
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
    enq(q, JobKind.NOOP)
    j = _run(q, TransientError("later"))
    assert j.status is JobState.RETRY_WAIT and j.last_error == "later"
    assert q.reap() == 0
    clock.advance(backoff(1) + timedelta(seconds=1))
    assert q.reap() == 1
    assert q.get(j.id).status is JobState.QUEUED


def test_transient_error_fails_when_attempts_exhausted(q: JobQueue) -> None:
    enq(q, JobKind.NOOP, max_attempts=1)
    assert _run(q, TransientError("x")).status is JobState.FAILED


def test_permanent_error_fails(q: JobQueue) -> None:
    enq(q, JobKind.NOOP)
    assert _run(q, PermanentError("no")).status is JobState.FAILED


@pytest.mark.parametrize("exc", [TransientError("x"), RuntimeError("bug")])
def test_handler_error_after_job_deleted_does_not_raise(
    q: JobQueue, engine: Engine, exc: Exception
) -> None:
    enq(q, JobKind.NOOP)

    async def delete_then_boom(j: JobRecord) -> None:
        with Session(engine) as s, s.begin():
            s.delete(s.get(Job, j.id))
        raise exc

    job = q.claim("w", LEASE)
    assert job is not None
    asyncio.run(run_job(q, job, "w", {JobKind.NOOP: delete_then_boom}, LEASE))  # no LeaseLostError


class Failures:
    """Records what the queue hands to its final-failure hook."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, Any], str]] = []
        self.states: list[JobState] = []  # the job's status as the hook sees it

    def __call__(
        self, c: Connection, job_id: int, payload: dict[str, Any], error: str, _now: datetime
    ) -> None:
        self.calls.append((job_id, payload, error))
        self.states.append(c.execute(select(Job.status).where(Job.id == job_id)).scalar_one())


@pytest.fixture
def failures() -> Failures:
    return Failures()


@pytest.fixture
def fq(engine: Engine, clock: FakeClock, failures: Failures) -> JobQueue:
    return JobQueue(engine, clock, failures)


@pytest.mark.parametrize("payload", [{"request_id": 7}, {}])  # a job without a request too
def test_permanent_failure_hands_the_job_to_the_hook(
    fq: JobQueue, failures: Failures, payload: dict[str, Any]
) -> None:
    j = enq(fq, JobKind.NOOP, payload)
    _run(fq, PermanentError("404"))
    assert failures.calls == [(j.id, payload, "PermanentError('404')")]


def test_the_hook_commits_or_rolls_back_with_the_state_change(
    engine: Engine, clock: FakeClock
) -> None:
    seen: list[JobState] = []

    def hook(c: Connection, job_id: int, *_: Any) -> None:
        seen.append(c.execute(select(Job.status).where(Job.id == job_id)).scalar_one())

    q = JobQueue(engine, clock, hook)
    enq(q, JobKind.NOOP)
    _run(q, PermanentError("x"))
    assert seen == [JobState.FAILED]

    def boom(*_: Any) -> None:
        raise RuntimeError("hook failed")

    q2 = JobQueue(engine, clock, boom)
    j = enq(q2, JobKind.NOOP)
    assert (job := q2.claim("w2", LEASE)) and job.id == j.id
    with pytest.raises(RuntimeError):
        q2.fail(j.id, "w2", "e")
    assert q2.get(j.id).status is JobState.LEASED  # rolled back with the hook


def test_a_retry_does_not_call_the_hook(fq: JobQueue, failures: Failures) -> None:
    enq(fq, JobKind.NOOP, {"request_id": 7})
    _run(fq, TransientError("later"))
    assert failures.calls == []


def test_reaped_dead_job_is_handed_to_the_hook(
    fq: JobQueue, failures: Failures, clock: FakeClock
) -> None:
    j = enq(fq, JobKind.NOOP, {"request_id": 7}, max_attempts=1)
    fq.claim("w1", LEASE)
    fq.start(j.id, "w1")
    clock.advance(LEASE + timedelta(seconds=1))
    assert fq.reap() == 1
    assert fq.get(j.id).status is JobState.FAILED
    assert failures.calls == [(j.id, {"request_id": 7}, "lease expired with no attempts left")]
    assert failures.states == [JobState.FAILED]  # the hook runs in the same transaction


def _locked() -> OperationalError:
    return OperationalError("UPDATE jobs", {}, Exception("database is locked"))


def test_a_locked_database_in_a_handler_retries_instead_of_failing(q: JobQueue) -> None:
    enq(q, JobKind.NOOP)
    assert _run(q, _locked()).status is JobState.RETRY_WAIT


def test_a_locked_database_while_recording_the_outcome_leaves_the_job_for_reap(
    q: JobQueue, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    j = enq(q, JobKind.NOOP)
    job = q.claim("w", LEASE)
    assert job is not None

    def locked(*_a: object, **_k: object) -> None:
        raise _locked()

    for name in ("complete", "retry", "fail"):
        monkeypatch.setattr(q, name, locked)
    asyncio.run(run_job(q, job, "w", HANDLERS, LEASE))  # must not raise into the worker loop
    assert q.get(j.id).status is JobState.RUNNING
    clock.advance(LEASE + timedelta(seconds=1))
    assert q.reap() == 1 and q.get(j.id).status is JobState.QUEUED


def test_a_locked_database_while_completing_never_reruns_finished_work(
    q: JobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    j = enq(q, JobKind.NOOP)
    job = q.claim("w", LEASE)
    assert job is not None
    runs: list[int] = []
    real, locks = q.complete, [_locked()]

    async def handler(_job: object) -> None:
        runs.append(1)

    def complete(*a: object, **k: object) -> None:
        if locks:
            raise locks.pop()
        real(*a, **k)  # type: ignore[arg-type]

    def retry(*_a: object, **_k: object) -> None:
        raise AssertionError("finished work must not be retried")

    monkeypatch.setattr(q, "complete", complete)
    monkeypatch.setattr(q, "retry", retry)
    asyncio.run(run_job(q, job, "w", {JobKind.NOOP: handler}, LEASE))
    assert runs == [1] and q.get(j.id).status is JobState.SUCCEEDED


def _complete_raises(
    q: JobQueue, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> tuple[int, list[int]]:
    """Claim a job, make complete() raise `exc`, and fail the test if it is retried or failed."""
    j = enq(q, JobKind.NOOP)
    job = q.claim("w", LEASE)
    assert job is not None
    calls: list[int] = []

    async def handler(_job: object) -> None:
        pass

    def complete(*_a: object, **_k: object) -> None:
        calls.append(1)
        raise exc

    def never(*_a: object, **_k: object) -> None:
        raise AssertionError("finished work must not be retried or failed")

    monkeypatch.setattr(worker, "COMPLETE_PAUSE", 0)
    monkeypatch.setattr(q, "complete", complete)
    monkeypatch.setattr(q, "retry", never)
    monkeypatch.setattr(q, "fail", never)
    asyncio.run(run_job(q, job, "w", {JobKind.NOOP: handler}, LEASE))  # must not raise
    return j.id, calls


@pytest.mark.parametrize(
    ("make_exc", "tries", "logged"),
    [
        (lambda: ValueError("bad transition"), 1, None),
        (_locked, worker.COMPLETE_TRIES, "will run again after the lease expires"),
        (LeaseLostError, 1, "lost lease"),
    ],
)
def test_an_error_while_completing_leaves_the_job_for_reap(
    q: JobQueue,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    make_exc: Callable[[], Exception],
    tries: int,
    logged: str | None,
) -> None:
    jid, calls = _complete_raises(q, monkeypatch, make_exc())
    assert len(calls) == tries and q.get(jid).status is JobState.RUNNING
    if logged:
        assert logged in caplog.text


def test_a_failed_heartbeat_keeps_beating() -> None:
    beats: list[int] = []

    class Queue:
        def heartbeat(self, *_a: object) -> bool:
            beats.append(1)
            if len(beats) == 1:
                raise _locked()
            return len(beats) < 3

    asyncio.run(_heartbeat(Queue(), 1, "w", timedelta(seconds=0)))  # type: ignore[arg-type]
    assert len(beats) == 3


def test_web_sessions_raise_only_default_priority(engine: Engine, clock: FakeClock) -> None:
    plain = make_session_factory(engine)
    for factory, prio, tag in (
        (plain, 0, "worker"),  # follow-on jobs keep their priority
        (user_sessions(plain), 0, "click"),
        (user_sessions(plain), 1, "migration"),  # explicit, like a confirm's bulk VERIFY jobs
    ):
        with factory() as s, s.begin():
            enqueue_in(s, clock.now(), JobKind.SEARCH, {"tag": tag}, priority=prio)
    with plain() as s:
        got = {j.payload["tag"]: j.priority for j in s.scalars(select(Job))}
    assert got == {"worker": 0, "click": USER_PRIORITY, "migration": 1}


def test_job_whose_lease_expires_while_leased_fails_after_max_attempts(
    q: JobQueue, clock: FakeClock
) -> None:
    j = enq(q, JobKind.NOOP, max_attempts=2)
    for _ in range(2):  # the worker dies between claim and start, every time
        assert q.claim("w", LEASE) is not None
        clock.advance(LEASE + timedelta(seconds=1))
        q.reap()
    assert q.get(j.id).status is JobState.FAILED
    assert q.claim("w", LEASE) is None
