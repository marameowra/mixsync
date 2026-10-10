import asyncio
import logging
import os
import signal
import socket
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.pipeline import Pipeline
from mixsync.core.clock import Clock, SystemClock
from mixsync.core.config import Settings
from mixsync.core.errors import TransientError
from mixsync.core.jobs import DEFAULT_LEASE, JobKind
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.journal import SqlJournal
from mixsync.db.queue import JobQueue, JobRecord, LeaseLostError
from mixsync.library.fileops import FileOps
from mixsync.library.importer import Importer
from mixsync.metadata.acoustid import AcoustIdClient, fingerprint
from mixsync.metadata.coverart import CoverArtClient
from mixsync.metadata.musicbrainz import MusicBrainzProvider
from mixsync.ratelimit.client import PoliteClient, polite_client
from mixsync.sources.query import queries
from mixsync.sources.slskd import SlskdSource
from mixsync.targets.navidrome import NavidromeTarget

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


def _pipeline(
    settings: Settings, fileops: FileOps, sessions: sessionmaker[Session], clock: Clock
) -> Pipeline:
    def client(service: str) -> PoliteClient:
        return polite_client(service, settings, sessions)

    return Pipeline(
        sessions=sessions,
        clock=clock,
        source=SlskdSource(
            client("slskd"),
            settings.slskd_url,
            settings.slskd_api_key,
            downloads_dir=Path(settings.downloads_dir),
        ),
        queries=queries,
        metadata=MusicBrainzProvider(client("musicbrainz")),
        fingerprint=fingerprint,
        acoustid=AcoustIdClient(client("acoustid"), settings),
        importer=Importer(fileops, sessions, clock, settings.path_template),
        coverart=CoverArtClient(client("coverart")),
        target=NavidromeTarget(
            client("navidrome"),
            settings.navidrome_url,
            settings.navidrome_user,
            settings.navidrome_password,
        ),
    )


def _new_queue() -> tuple[JobQueue, str, dict[JobKind, Handler]]:
    settings = Settings()
    engine = make_engine(settings)
    clock = SystemClock()
    sessions = make_session_factory(engine)
    fileops = FileOps(Path(settings.data_dir), SqlJournal(sessions, clock), clock)
    fileops.recover()
    queue = JobQueue(engine, clock)
    handlers = HANDLERS | _pipeline(settings, fileops, sessions, clock).handlers()
    return queue, f"{socket.gethostname()}:{os.getpid()}", handlers


async def serve_worker() -> None:
    queue, worker, handlers = _new_queue()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await run_worker(queue, worker, stop, handlers)


async def serve_all() -> None:
    """Web and worker in one process. uvicorn owns SIGTERM/SIGINT; the worker follows it."""
    import uvicorn

    queue, worker, handlers = _new_queue()
    stop = asyncio.Event()
    server = uvicorn.Server(
        uvicorn.Config("mixsync.app:create_app", factory=True, host="0.0.0.0", port=8080)
    )
    task = asyncio.create_task(run_worker(queue, worker, stop, handlers))
    try:
        await server.serve()
    finally:
        stop.set()
        await task
