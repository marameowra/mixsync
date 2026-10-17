"""A job that fails for good fails its request (the hook acquire registers with the queue)."""

from datetime import UTC, timedelta
from typing import Any

from mixsync.core.errors import PermanentError
from mixsync.core.jobs import JobKind
from mixsync.db.models.library import Track
from mixsync.db.models.work import Request
from mixsync.db.queue import JobRecord, enqueue_in
from mixsync.worker import run_job

from .conftest import LEASE, Env


def _request(env: Env, status: str, **kw: Any) -> int:
    with env.sessions.begin() as s:
        req = Request(user_id=env.user_id, release_mbid="m", status=status, **kw)
        s.add(req)
        s.flush()
        return req.id


def _enqueue(env: Env, rid: int, **kw: Any) -> None:
    with env.sessions.begin() as s:
        enqueue_in(s, env.clock.now(), JobKind.NOOP, {"request_id": rid}, **kw)


async def _fail_next(env: Env) -> None:
    async def boom(_j: JobRecord) -> None:
        raise PermanentError("404")

    job = env.queue.claim("w", LEASE)
    assert job
    await run_job(env.queue, job, "w", {JobKind.NOOP: boom}, LEASE)


def _get(env: Env, rid: int) -> Request:
    with env.sessions() as s:
        return s.get_one(Request, rid)


async def test_a_failing_job_fails_the_request_and_clears_dead_end(env: Env) -> None:
    rid = _request(env, "searching", dead_end=True)
    _enqueue(env, rid)
    await _fail_next(env)
    req = _get(env, rid)
    assert (req.status, req.last_error, req.dead_end) == ("failed", "PermanentError('404')", False)
    assert req.updated_at.replace(tzinfo=UTC) == env.clock.now()


async def test_a_failing_job_keeps_a_partial_request_partial(env: Env) -> None:
    rid = _request(env, "partial", partial_note="2 of 5 tracks present")
    with env.sessions.begin() as s:
        s.add(
            Track(
                path="a.mp3",
                status="verified",
                batch_id=f"request-{rid}-1",
                imported_at=env.clock.now(),
            )
        )
    _enqueue(env, rid)
    await _fail_next(env)
    req = _get(env, rid)
    assert req.status == "partial"
    assert req.last_error == "partial: 2 of 5 tracks present; PermanentError('404')"


async def test_a_request_that_moved_on_is_left_alone(env: Env) -> None:
    rid = _request(env, "imported")
    _enqueue(env, rid)
    await _fail_next(env)
    assert _get(env, rid).status == "imported"


async def test_a_superseded_job_failing_leaves_the_request(env: Env) -> None:
    rid = _request(env, "no_results")
    _enqueue(env, rid)
    _enqueue(env, rid)  # the newer job now drives the request
    await _fail_next(env)  # fails the older job
    assert _get(env, rid).status == "no_results"


async def test_newer_jobs_of_other_requests_do_not_shield_the_request(env: Env) -> None:
    rid = _request(env, "searching")
    _enqueue(env, rid)
    _enqueue(env, rid + 1)
    await _fail_next(env)  # claims the first job
    assert _get(env, rid).status == "failed"


async def test_a_reaped_dead_job_fails_the_request(env: Env) -> None:
    rid = _request(env, "verifying")
    _enqueue(env, rid, max_attempts=1)
    job = env.queue.claim("w1", LEASE)
    assert job
    env.queue.start(job.id, "w1")
    env.clock.t += LEASE + timedelta(seconds=1)
    env.queue.reap()
    assert _get(env, rid).status == "failed"


async def test_newest_job_failing_fails_despite_an_older_one(env: Env) -> None:
    rid = _request(env, "searching")
    _enqueue(env, rid, run_after=env.clock.now() + timedelta(hours=1))
    _enqueue(env, rid)
    await _fail_next(env)  # claims the newer, runnable job
    assert _get(env, rid).status == "failed"


async def test_permanent_failure_fails_a_no_results_request(env: Env) -> None:
    rid = _request(env, "no_results")
    _enqueue(env, rid)
    await _fail_next(env)
    assert _get(env, rid).status == "failed"
