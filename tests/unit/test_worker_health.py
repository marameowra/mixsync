"""Startup logging, the worker's liveness heartbeat and the `worker-health` command."""

import asyncio
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from mixsync.__main__ import configure_logging, main
from mixsync.core.config import Settings
from mixsync.core.jobs import WORKER_STALE_AFTER, JobKind
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.queue import JobQueue, JobRecord, enqueue_in
from mixsync.db.queue import worker_age as age_of
from mixsync.worker import run_worker


class FakeClock:
    def __init__(self) -> None:
        self.t = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.t


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def worker_age(settings: Settings, now: datetime) -> timedelta | None:
    with make_session_factory(make_engine(settings))() as s:
        return age_of(s, now)


async def one_round(queue: JobQueue, handlers: dict[JobKind, object] | None = None) -> None:
    stop = asyncio.Event()
    claim = queue.claim

    def claim_then_stop(*a: object) -> JobRecord | None:
        stop.set()
        return claim(*a)  # type: ignore[arg-type]

    queue.claim = claim_then_stop  # type: ignore[method-assign]
    await run_worker(queue, "w", stop, handlers or {}, poll=0)  # type: ignore[arg-type]


def test_log_level_defaults_to_info_and_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings().log_level == "INFO"
    monkeypatch.setenv("MIXSYNC_LOG_LEVEL", "debug")
    assert configure_logging() == "DEBUG"
    assert logging.getLogger().level == logging.DEBUG


def test_an_invalid_log_level_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIXSYNC_LOG_LEVEL", "LOUD")
    with pytest.raises(ValidationError, match="log_level"):
        main(["worker"])


async def test_job_start_and_finish_reach_stderr_at_the_default_level(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    upgrade()
    configure_logging()
    engine = make_engine(settings)
    clock = FakeClock()
    queue = JobQueue(engine, clock, lambda *_: None)
    with Session(engine) as s, s.begin():
        enqueue_in(s, clock.now(), JobKind.NOOP, {})

    async def ok(_job: JobRecord) -> None:
        return None

    await one_round(queue, {JobKind.NOOP: ok})
    err = capsys.readouterr().err
    assert " INFO mixsync.worker: job 1 (noop) started" in err
    assert "job 1 finished" in err


async def test_health_is_fresh_after_a_round_and_stale_past_the_threshold(
    settings: Settings,
) -> None:
    upgrade()
    clock = FakeClock()
    assert worker_age(settings, clock.now()) is None
    await one_round(JobQueue(make_engine(settings), clock, lambda *_: None))
    assert worker_age(settings, clock.now()) == timedelta(0)
    later = clock.now() + WORKER_STALE_AFTER + timedelta(seconds=1)
    assert (worker_age(settings, later) or timedelta(0)) > WORKER_STALE_AFTER


async def test_worker_health_command_exit_codes(settings: Settings) -> None:
    upgrade()
    assert main(["worker-health"]) == 1  # never written
    clock = FakeClock()
    clock.t = datetime.now(UTC)
    await one_round(JobQueue(make_engine(settings), clock, lambda *_: None))
    assert main(["worker-health"]) == 0
    clock.t -= WORKER_STALE_AFTER + timedelta(seconds=5)
    await one_round(JobQueue(make_engine(settings), clock, lambda *_: None))
    assert main(["worker-health"]) == 1


async def test_a_failed_heartbeat_write_does_not_stop_the_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    claims = 0
    stop = asyncio.Event()

    class Queue:
        def alive(self, _worker: str) -> None:
            raise RuntimeError("database is locked")

        def reap(self) -> None:
            pass

        def claim(self, *_a: object) -> None:
            nonlocal claims
            claims += 1
            if claims == 2:
                stop.set()

    await run_worker(Queue(), "w", stop, {}, poll=0)  # type: ignore[arg-type]
    assert claims == 2
    assert "recording the worker heartbeat failed" in caplog.text
