"""The worker loop: startup tasks, polling and shutdown."""

import asyncio

from sqlalchemy.exc import OperationalError

from mixsync.worker import run_worker


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
