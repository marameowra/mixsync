import asyncio
import logging
import os
import signal
import socket
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.acoustid_submit import AcoustIdSubmitter
from mixsync.acquire.gate import DownloadGate
from mixsync.acquire.merge import Merger
from mixsync.acquire.migration import Migrator
from mixsync.acquire.pipeline import Pipeline
from mixsync.acquire.recheck import Rechecker
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
from mixsync.watchlist.poller import Watcher

log = logging.getLogger(__name__)

Handler = Callable[[JobRecord], Awaitable[None]]


async def _noop(_job: JobRecord) -> None:
    return None


HANDLERS: dict[JobKind, Handler] = {JobKind.NOOP: _noop}


async def _heartbeat(queue: JobQueue, job_id: int, worker: str, lease: timedelta) -> None:
    while True:
        await asyncio.sleep(lease.total_seconds() / 3)
        try:
            if not queue.heartbeat(job_id, worker, lease):
                return
        except Exception:  # a dead heartbeat lets the lease expire and the job run twice
            log.exception("heartbeat for job %s failed; trying again", job_id)


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
    except Exception as e:
        try:
            # A locked database is momentary, never a reason to fail finished work.
            if isinstance(e, TransientError | OperationalError):
                queue.retry(job.id, worker, str(e))
            else:  # PermanentError, SafetyError, bugs: never retried
                log.exception("job %s failed", job.id)
                queue.fail(job.id, worker, repr(e))
        except LeaseLostError:  # the job row was deleted or reaped while the handler ran
            log.warning("lost lease on job %s", job.id)
        except Exception:  # the bookkeeping failed too: reap() requeues it once the lease expires
            log.exception("could not record the outcome of job %s", job.id)
    finally:
        hb.cancel()


async def run_worker(
    queue: JobQueue,
    worker: str,
    stop: asyncio.Event,
    handlers: dict[JobKind, Handler] = HANDLERS,
    lease: timedelta = DEFAULT_LEASE,
    poll: float = 1.0,
    startup: Callable[[], None] | None = None,
) -> None:
    if startup:  # once the worker is up, off the event loop and the web start
        try:
            await asyncio.to_thread(startup)
        except Exception:  # a failed scan must never stop the worker
            log.exception("startup scan failed; the worker runs without it")
    # shortcut: queue calls are sync and short, run on the loop; move to threads if they block it.
    while not stop.is_set():
        try:
            queue.reap()
            job = queue.claim(worker, lease)
        except Exception:  # e.g. "database is locked": a dead loop leaves every job queued
            log.exception("polling the job queue failed; trying again")
            job = None
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
        gate=DownloadGate(
            settings.max_concurrent_downloads,
            timedelta(seconds=settings.slow_download_window),
            settings.slow_download_floor,
            settings.slow_download_share,
        ),
        queries=queries,
        metadata=MusicBrainzProvider(client("musicbrainz")),
        fingerprint=fingerprint,
        acoustid=AcoustIdClient(client("acoustid"), settings),
        importer=Importer(fileops, sessions, clock, settings.path_template),
        canonical_media=tuple(settings.canonical_media),
        canonical_countries=tuple(settings.canonical_countries),
        coverart=CoverArtClient(client("coverart")),
        target=NavidromeTarget(
            client("navidrome"),
            settings.navidrome_url,
            settings.navidrome_user,
            settings.navidrome_password,
        ),
    )


def _new_queue() -> tuple[JobQueue, str, dict[JobKind, Handler], Callable[[], None]]:
    settings = Settings()
    engine = make_engine(settings)
    clock = SystemClock()
    sessions = make_session_factory(engine)
    fileops = FileOps(
        Path(settings.data_dir), Path(settings.downloads_dir), SqlJournal(sessions, clock), clock
    )
    fileops.recover()
    queue = JobQueue(engine, clock)
    merger = Merger(
        sessions,
        clock,
        MusicBrainzProvider(polite_client("musicbrainz", settings, sessions)),
        fileops,
        settings.path_template,
        tuple(settings.canonical_media),
        tuple(settings.canonical_countries),
    )
    pipeline = _pipeline(settings, fileops, sessions, clock)
    rechecker = Rechecker(
        sessions,
        clock,
        pipeline.metadata,
        pipeline.fingerprint,
        pipeline.acoustid,
        fileops,
        settings.path_template,
    )
    submitter = AcoustIdSubmitter(
        sessions,
        clock,
        fileops,
        fingerprint,
        AcoustIdClient(polite_client("acoustid", settings, sessions), settings),
        bool(settings.acoustid_app_key and settings.acoustid_user_key),
    )
    migrator = Migrator(
        sessions,
        clock,
        Path(settings.import_source_dir),
        Path(settings.config_dir),
        Path(settings.data_dir) / "library",
        settings.mb_rate,
    )
    watcher = Watcher(
        sessions,
        clock,
        MusicBrainzProvider(polite_client("musicbrainz", settings, sessions)),
        media=tuple(settings.canonical_media),
        countries=tuple(settings.canonical_countries),
    )
    handlers = (
        HANDLERS
        | pipeline.handlers()
        | migrator.handlers()
        | merger.handlers()
        | rechecker.handlers()
        | submitter.handlers()
        | watcher.handlers()
    )

    def startup() -> None:
        rechecker.start_chains()
        watcher.start_chains()

    return queue, f"{socket.gethostname()}:{os.getpid()}", handlers, startup


async def serve_worker() -> None:
    queue, worker, handlers, startup = _new_queue()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await run_worker(queue, worker, stop, handlers, startup=startup)


async def serve_all() -> None:
    """Web and worker in one process. uvicorn owns SIGTERM/SIGINT; the worker follows it."""
    import uvicorn

    queue, worker, handlers, startup = _new_queue()
    stop = asyncio.Event()
    server = uvicorn.Server(
        uvicorn.Config("mixsync.app:create_app", factory=True, host="0.0.0.0", port=8080)
    )
    task = asyncio.create_task(run_worker(queue, worker, stop, handlers, startup=startup))

    def died(t: asyncio.Task[None]) -> None:  # never serve on without a worker: exit, restart
        if not t.cancelled() and (e := t.exception()):
            log.error("the worker stopped; shutting down", exc_info=e)
            server.should_exit = True

    task.add_done_callback(died)
    try:
        await server.serve()
    finally:
        stop.set()
        await task
