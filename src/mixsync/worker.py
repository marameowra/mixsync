import asyncio
import logging
import os
import signal
import socket
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

from mixsync.core.clock import SystemClock
from mixsync.core.config import Settings
from mixsync.core.errors import TransientError
from mixsync.core.jobs import DEFAULT_LEASE, JobKind
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.journal import SqlJournal
from mixsync.db.queue import JobQueue, JobRecord, LeaseLostError
from mixsync.library.fileops import FileOps

log = logging.getLogger(__name__)

Handler = Callable[[JobRecord], Awaitable[None]]


async def _noop(_job: JobRecord) -> None:
    return None


HANDLERS: dict[JobKind, Handler] = {JobKind.NOOP: _noop}


async def _heartbeat(queue: JobQueue, job_id: int, worker: str, lease: timedelta) -> None:
    while True:
        await asyncio.sleep(lease.total_seconds() / 3)
        if not queue.heartbeat(job_id, worker, lease):
            return


async def run_job(
    queue: JobQueue, job: JobRecord, worker: str, handlers: dict[JobKind, Handler], lease: timedelta
) -> None:
    hb = asyncio.create_task(_heartbeat(queue, job.id, worker, lease))
    try:
        queue.start(job.id, worker)
        await handlers[job.kind](job)
        queue.complete(job.id, worker)
    except LeaseLostError:
        log.warning("lost lease on job %s", job.id)
    except TransientError as e:
        queue.retry(job.id, worker, str(e))
    except Exception as e:  # PermanentError, SafetyError, bugs: never retried
        log.exception("job %s failed", job.id)
        queue.fail(job.id, worker, repr(e))
    finally:
        hb.cancel()


async def run_worker(
    queue: JobQueue,
    worker: str,
    stop: asyncio.Event,
    handlers: dict[JobKind, Handler] = HANDLERS,
    lease: timedelta = DEFAULT_LEASE,
    poll: float = 1.0,
) -> None:
    # shortcut: queue calls are sync and short, run on the loop; move to threads if they block it.
    while not stop.is_set():
        queue.reap()
        job = queue.claim(worker, lease)
        if job is None:
            try:
                await asyncio.wait_for(stop.wait(), poll)
            except TimeoutError:
                pass
            continue
        await run_job(queue, job, worker, handlers, lease)


def _new_queue() -> tuple[JobQueue, str]:
    settings = Settings()
    engine = make_engine(settings)
    clock = SystemClock()
    FileOps(
        Path(settings.data_dir), SqlJournal(make_session_factory(engine), clock), clock
    ).recover()
    queue = JobQueue(engine, clock)
    return queue, f"{socket.gethostname()}:{os.getpid()}"


async def serve_worker() -> None:
    queue, worker = _new_queue()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await run_worker(queue, worker, stop)


async def serve_all() -> None:
    """Web and worker in one process. uvicorn owns SIGTERM/SIGINT; the worker follows it."""
    import uvicorn

    queue, worker = _new_queue()
    stop = asyncio.Event()
    server = uvicorn.Server(
        uvicorn.Config("mixsync.app:create_app", factory=True, host="0.0.0.0", port=8080)
    )
    task = asyncio.create_task(run_worker(queue, worker, stop))
    try:
        await server.serve()
    finally:
        stop.set()
        await task
