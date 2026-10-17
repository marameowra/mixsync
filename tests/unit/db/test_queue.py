import asyncio
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from mixsync import worker
from mixsync.core.config import Settings
from mixsync.core.errors import PermanentError, TransientError
from mixsync.core.jobs import JobKind, JobState, backoff
from mixsync.db.engine import make_engine, make_session_factory, user_sessions
from mixsync.db.migrate import upgrade
from mixsync.db.models.auth import User
from mixsync.db.models.work import Job, Request
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


@pytest.fixture
def q(engine: Engine, clock: FakeClock) -> JobQueue:
    return JobQueue(engine, clock)


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
    q1, q2 = JobQueue(engine, clock), JobQueue(make_engine(), clock)  # two connections, file DB
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


def test_job_that_keeps_killing_its_worker_fails(q: JobQueue, clock: FakeClock) -> None:
    j = enq(q, JobKind.NOOP, max_attempts=1)
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
    enq(q, JobKind.NOOP, {"request_id": rid})
    _run(q, PermanentError("404"))
    assert _status(engine, rid) == ("failed", "PermanentError('404')")


def test_a_job_failure_is_not_a_dead_end(q: JobQueue, engine: Engine) -> None:
    rid = _request(engine, "searching")
    with Session(engine) as s, s.begin():
        s.get_one(Request, rid).dead_end = True
    enq(q, JobKind.NOOP, {"request_id": rid})
    _run(q, PermanentError("404"))
    with Session(engine) as s:
        assert not s.get_one(Request, rid).dead_end


def test_failure_leaves_a_request_that_moved_on(q: JobQueue, engine: Engine) -> None:
    rid = _request(engine, "imported")
    enq(q, JobKind.NOOP, {"request_id": rid})
    _run(q, PermanentError("stale"))
    assert _status(engine, rid) == ("imported", None)


def test_reaped_dead_job_fails_the_request(q: JobQueue, engine: Engine, clock: FakeClock) -> None:
    rid = _request(engine, "verifying")
    j = enq(q, JobKind.NOOP, {"request_id": rid}, max_attempts=1)
    q.claim("w1", LEASE)
    q.start(j.id, "w1")
    clock.advance(LEASE + timedelta(seconds=1))
    q.reap()
    assert _status(engine, rid)[0] == "failed"


def test_permanent_failure_fails_a_no_results_request(q: JobQueue, engine: Engine) -> None:
    rid = _request(engine, "no_results")
    enq(q, JobKind.NOOP, {"request_id": rid})
    _run(q, PermanentError("gone"))
    assert _status(engine, rid)[0] == "failed"


def test_failure_of_a_superseded_job_leaves_the_request(q: JobQueue, engine: Engine) -> None:
    rid = _request(engine, "no_results")
    enq(q, JobKind.NOOP, {"request_id": rid})
    enq(q, JobKind.NOOP, {"request_id": rid})  # the newer job now drives the request
    _run(q, PermanentError("stale"))  # fails the older job
    assert _status(engine, rid) == ("no_results", None)


def test_reaped_superseded_job_leaves_the_request(
    q: JobQueue, engine: Engine, clock: FakeClock
) -> None:
    rid = _request(engine, "no_results")
    j = enq(q, JobKind.NOOP, {"request_id": rid}, max_attempts=1)
    enq(q, JobKind.NOOP, {"request_id": rid})
    q.claim("w1", LEASE)
    q.start(j.id, "w1")
    clock.advance(LEASE + timedelta(seconds=1))
    q.reap()
    assert q.get(j.id).status is JobState.FAILED
    assert _status(engine, rid) == ("no_results", None)


def test_newer_jobs_of_other_requests_do_not_shield_the_request(
    q: JobQueue, engine: Engine
) -> None:
    rid = _request(engine, "searching")
    enq(q, JobKind.NOOP, {"request_id": rid})
    enq(q, JobKind.NOOP, {"request_id": rid + 1})
    enq(q, JobKind.NOOP, {})
    _run(q, PermanentError("boom"))  # claims the first job
    assert _status(engine, rid)[0] == "failed"


def test_newest_job_failing_fails_despite_an_older_one(
    q: JobQueue, engine: Engine, clock: FakeClock
) -> None:
    rid = _request(engine, "searching")
    enq(q, JobKind.NOOP, {"request_id": rid}, run_after=clock.now() + timedelta(hours=1))
    enq(q, JobKind.NOOP, {"request_id": rid})
    _run(q, PermanentError("boom"))  # claims the newer, runnable job
    assert _status(engine, rid)[0] == "failed"


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


def test_any_error_while_completing_leaves_the_job_for_reap(
    q: JobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    jid, calls = _complete_raises(q, monkeypatch, ValueError("bad transition"))
    assert calls == [1] and q.get(jid).status is JobState.RUNNING


def test_exhausted_completion_retries_say_the_job_will_run_again(
    q: JobQueue, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    jid, calls = _complete_raises(q, monkeypatch, _locked())
    assert len(calls) == worker.COMPLETE_TRIES and q.get(jid).status is JobState.RUNNING
    assert "will run again after the lease expires" in caplog.text


def test_a_lost_lease_while_completing_is_only_logged(
    q: JobQueue, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    jid, calls = _complete_raises(q, monkeypatch, LeaseLostError())
    assert calls == [1] and q.get(jid).status is JobState.RUNNING
    assert "lost lease" in caplog.text


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
