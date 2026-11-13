"""The worker loop: startup tasks, polling and shutdown."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import SystemClock
from mixsync.core.config import Settings
from mixsync.core.jobs import JobKind, JobState
from mixsync.db.app_settings import restart_requested
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.auth import AppSettings
from mixsync.db.queue import JobQueue, JobRecord, enqueue_in
from mixsync.wiring import build_services
from mixsync.worker import restart_check, run_worker


async def test_startup_runs_in_the_worker_task_before_the_first_claim() -> None:
    order: list[str] = []

    class Queue:
        def reap(self) -> None:
            order.append("reap")

        def claim(self, *_a: object) -> None:
            stop.set()

    stop = asyncio.Event()
    await run_worker(Queue(), "w", stop, {}, startup=(lambda: order.append("startup"),))  # type: ignore[arg-type]
    assert order == ["startup", "reap"]


async def test_failed_startup_retries_only_failed_chains_while_polling() -> None:
    order: list[str] = []
    attempts = 0
    claims = 0
    stop = asyncio.Event()

    def first() -> None:
        nonlocal attempts
        attempts += 1
        order.append("first")
        if attempts == 1:
            raise OperationalError("startup", {}, RuntimeError("database is locked"))

    class Queue:
        def reap(self) -> None:
            pass

        def claim(self, *_a: object) -> None:
            nonlocal claims
            claims += 1
            order.append("claim")
            if claims == 3:
                stop.set()

    await run_worker(
        Queue(),  # type: ignore[arg-type] -- only the queue methods used by an idle worker
        "w",
        stop,
        {},
        poll=0,
        startup=(first, lambda: order.append("second"), lambda: order.append("third")),
    )
    assert order == ["first", "second", "third", "claim", "first", "claim", "claim"]
    assert attempts == 2
    assert claims == 3


async def test_shutdown_during_startup_stops_initialization_and_polling() -> None:
    order: list[str] = []
    stop = asyncio.Event()

    def shutdown() -> None:
        order.append("startup")
        stop.set()

    class Queue:
        def reap(self) -> None:
            order.append("reap")

        def claim(self, *_a: object) -> None:
            order.append("claim")

    await run_worker(
        Queue(),  # type: ignore[arg-type] -- only the queue methods used by an idle worker
        "w",
        stop,
        {},
        startup=(shutdown, lambda: order.append("other")),
    )
    assert order == ["startup"]


async def test_a_locked_database_while_polling_does_not_stop_the_worker() -> None:
    calls: list[str] = []

    class Queue:
        def reap(self) -> None:
            calls.append("reap")
            if len(calls) == 1:
                raise RuntimeError("database is locked")

        def claim(self, *_a: object) -> None:
            calls.append("claim")
            stop.set()

    stop = asyncio.Event()
    await run_worker(Queue(), "w", stop, {}, poll=0)  # type: ignore[arg-type]
    assert calls == ["reap", "reap", "claim"]


async def test_a_held_write_lock_does_not_freeze_the_event_loop(settings: Settings) -> None:
    upgrade()
    engine = make_engine(settings)
    queue = JobQueue(
        engine, type("C", (), {"now": lambda _s: datetime.now(UTC)})(), lambda *_: None
    )
    holder = sqlite3.connect(Path(engine.url.database or ""), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    stop = asyncio.Event()
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.05)
            ticks += 1

    async def release_then_stop() -> None:
        nonlocal held_ticks
        await asyncio.sleep(0.5)
        held_ticks = ticks
        holder.rollback()
        stop.set()

    held_ticks = 0
    try:
        await asyncio.gather(
            run_worker(queue, "w", stop, {}, poll=0.05), ticker(), release_then_stop()
        )
    finally:
        holder.close()
        engine.dispose()
    assert held_ticks >= 5  # a blocked loop would manage ~0 ticks while the lock was held


def _request_restart(sf: sessionmaker[Session], at: datetime) -> None:
    with sf() as s, s.begin():
        s.merge(AppSettings(id=1, restart_requested_at=at))


async def test_a_restart_request_lets_the_running_job_finish_then_stops_the_worker(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mixsync.worker.ALIVE_EVERY", 0)  # check on every turn of the loop
    upgrade()
    engine = make_engine(settings)
    sf = make_session_factory(engine)
    clock = SystemClock()
    started = clock.now()
    with sf() as s, s.begin():
        enqueue_in(s, started, JobKind.NOOP, {})
        enqueue_in(s, started, JobKind.NOOP, {})
    ran: list[int] = []

    async def job(record: JobRecord) -> None:  # Apply is pressed while this job runs
        _request_restart(sf, datetime.now(UTC))
        await asyncio.sleep(0.05)
        ran.append(record.id)

    queue = JobQueue(engine, clock, lambda *_: None)
    stop = asyncio.Event()
    restart = lambda: restart_requested(sf, started)  # noqa: E731
    await asyncio.wait_for(
        run_worker(queue, "w", stop, {JobKind.NOOP: job}, poll=0, restart=restart), 10
    )
    assert not stop.is_set() and len(ran) == 1  # returned by itself, after one whole job
    assert queue.get(ran[0]).status == JobState.SUCCEEDED
    engine.dispose()


async def test_a_worker_started_after_the_restart_request_keeps_running(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mixsync.worker.ALIVE_EVERY", 0)
    upgrade()
    sf = make_session_factory(make_engine(settings))
    _request_restart(sf, datetime.now(UTC) - timedelta(minutes=1))
    claims = 0

    class Queue:
        def alive(self, _w: str) -> None:
            pass

        def reap(self) -> None:
            pass

        def claim(self, *_a: object) -> None:
            nonlocal claims
            claims += 1
            if claims == 3:
                stop.set()

    stop = asyncio.Event()
    started = datetime.now(UTC)
    await run_worker(Queue(), "w", stop, {}, poll=0, restart=lambda: restart_requested(sf, started))  # type: ignore[arg-type]
    assert claims == 3  # polled on until stopped by hand


@pytest.mark.parametrize("on", [True, False])
def test_a_stored_restart_request_stops_a_process_only_with_self_restart(
    settings: Settings, on: bool
) -> None:
    upgrade()
    sf = make_session_factory(make_engine(settings))
    restart = restart_check(build_services(Settings(self_restart=on), sf))
    _request_restart(sf, datetime.now(UTC) + timedelta(seconds=1))
    assert restart() is on
