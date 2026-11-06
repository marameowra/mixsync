import asyncio
import logging
import os
import signal
import socket
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.acoustid_submit import AcoustIdSubmitter
from mixsync.acquire.adopter import adopter
from mixsync.acquire.dropfolder import DropFolder
from mixsync.acquire.gate import DownloadGate
from mixsync.acquire.migration import Migrator
from mixsync.acquire.pipeline import Pipeline
from mixsync.acquire.recheck import Rechecker
from mixsync.acquire.requests import fail_request_of_job
from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.core.errors import TransientError
from mixsync.core.jobs import DEFAULT_LEASE, JobKind, backoff
from mixsync.db.backup import NightlyBackup
from mixsync.db.queue import JobQueue, JobRecord, LeaseLostError
from mixsync.discovery.listenbrainz import ListenBrainzClient
from mixsync.discovery.playlists import Discovery
from mixsync.discovery.retention import Retention
from mixsync.library import tag_undo
from mixsync.library.fileops import FileOps
from mixsync.library.importer import Importer
from mixsync.library.name_backfill import NameBackfill
from mixsync.library.purge import TrashPurger
from mixsync.metadata.acoustid import AcoustIdClient, fingerprint
from mixsync.ratelimit.client import PoliteClient
from mixsync.sources.query import queries
from mixsync.sources.slskd import SlskdSource
from mixsync.targets.navidrome import NavidromeTarget, NavidromeUser
from mixsync.watchlist.poller import Watcher
from mixsync.wiring import Services, build_services

log = logging.getLogger(__name__)

COMPLETE_TRIES = 4
ALIVE_EVERY = 10.0  # seconds between idle liveness writes; well inside WORKER_STALE_AFTER
COMPLETE_PAUSE = 0.2  # seconds, times the attempt number

Handler = Callable[[JobRecord], Awaitable[None]]


async def _noop(_job: JobRecord) -> None:
    return None


HANDLERS: dict[JobKind, Handler] = {JobKind.NOOP: _noop}


async def _alive(queue: JobQueue, worker: str) -> None:
    try:
        await asyncio.to_thread(queue.alive, worker)
    except Exception:  # liveness is for the healthcheck; never stop polling or a job over it
        log.exception("recording the worker heartbeat failed; trying again")


async def _heartbeat(queue: JobQueue, job_id: int, worker: str, lease: timedelta) -> None:
    while True:
        await asyncio.sleep(lease.total_seconds() / 3)
        await _alive(queue, worker)  # a long job blocks the polling loop
        try:
            if not await asyncio.to_thread(queue.heartbeat, job_id, worker, lease):
                return
        except Exception:  # a dead heartbeat lets the lease expire and the job run twice
            log.exception("heartbeat for job %s failed; trying again", job_id)


async def _complete(queue: JobQueue, job_id: int, worker: str) -> None:
    """The work is done: a locked database must never send it to retry (it would run twice)."""
    for attempt in range(COMPLETE_TRIES):
        try:
            await asyncio.to_thread(queue.complete, job_id, worker)
            return
        except LeaseLostError:
            log.warning("lost lease on job %s", job_id)
            return
        except OperationalError:
            if attempt == COMPLETE_TRIES - 1:  # reap() requeues it once the lease expires
                log.exception(
                    "job %s finished but its success was not recorded; "
                    "it will run again after the lease expires",
                    job_id,
                )
                return
            await asyncio.sleep(COMPLETE_PAUSE * (attempt + 1))
        except Exception:  # never take the worker down; reap() requeues it after the lease
            log.exception(
                "job %s finished but its success was not recorded; "
                "it will run again after the lease expires",
                job_id,
            )
            return


async def run_job(
    queue: JobQueue, job: JobRecord, worker: str, handlers: dict[JobKind, Handler], lease: timedelta
) -> None:
    hb = asyncio.create_task(_heartbeat(queue, job.id, worker, lease))
    try:
        await asyncio.to_thread(queue.start, job.id, worker)
        log.info("job %s (%s) started", job.id, job.kind)
        await handlers[job.kind](job)
    except LeaseLostError:
        log.warning("lost lease on job %s", job.id)
    except Exception as e:
        try:
            # A locked database is momentary, never a reason to fail finished work.
            if isinstance(e, TransientError | OperationalError):
                state = await asyncio.to_thread(queue.retry, job.id, worker, str(e))
                log.warning("job %s failed (%s): %s", job.id, state, e)
            else:  # PermanentError, SafetyError, bugs: never retried
                log.exception("job %s failed", job.id)
                await asyncio.to_thread(queue.fail, job.id, worker, repr(e))
        except LeaseLostError:  # the job row was deleted or reaped while the handler ran
            log.warning("lost lease on job %s", job.id)
        except Exception:  # the bookkeeping failed too: reap() requeues it once the lease expires
            log.exception("could not record the outcome of job %s", job.id)
    else:
        await _complete(queue, job.id, worker)
        log.info("job %s finished", job.id)
    finally:
        hb.cancel()


async def run_worker(
    queue: JobQueue,
    worker: str,
    stop: asyncio.Event,
    handlers: dict[JobKind, Handler] = HANDLERS,
    lease: timedelta = DEFAULT_LEASE,
    poll: float = 1.0,
    startup: tuple[Callable[[], None], ...] = (),
) -> None:
    pending = list(startup)
    loop = asyncio.get_running_loop()
    retry_at = alive_at = loop.time()
    while not stop.is_set():
        if loop.time() >= alive_at:  # not every poll: an idle SQLite worker would write each second
            await _alive(queue, worker)
            alive_at = loop.time() + ALIVE_EVERY
        if pending and loop.time() >= retry_at:
            for initialize in pending.copy():
                if stop.is_set():
                    return
                try:
                    await asyncio.to_thread(initialize)
                except Exception:  # one failed chain must not block polling or the other chains
                    log.exception("startup scan %s failed; trying again", initialize)
                else:
                    pending.remove(initialize)
            retry_at = loop.time() + poll
        if stop.is_set():
            return
        try:
            await asyncio.to_thread(queue.reap)
            job = await asyncio.to_thread(queue.claim, worker, lease)
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


def _pipeline(services: Services, acoustid: AcoustIdClient) -> Pipeline:
    settings, sessions, clock = services.settings, services.session_factory, services.clock

    return Pipeline(
        sessions=sessions,
        clock=clock,
        source=SlskdSource(
            PoliteClient("slskd", settings, sessions),
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
        metadata=services.musicbrainz,
        fingerprint=fingerprint,
        acoustid=acoustid,
        importer=Importer(services.fileops, sessions, clock, settings.path_template),
        reencode=True,
        flac_level=settings.flac_level,
        canonical_media=tuple(settings.canonical_media),
        canonical_countries=tuple(settings.canonical_countries),
        coverart=services.coverart,
        target=NavidromeTarget(
            services.navidrome_http,
            settings.navidrome_url,
            settings.navidrome_user,
            settings.navidrome_password,
        ),
    )


RECOVERY_ATTEMPTS = 7  # delays 5+10+20+30+30+30 s: about 2 minutes in total


def _recover(
    fileops: FileOps,
    sessions: sessionmaker[Session],
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Startup recovery, retried while the database is locked. Idempotent, so safe to re-run."""
    for attempt in range(1, RECOVERY_ATTEMPTS + 1):
        try:
            fileops.recover()
            tag_undo.recover(sessions, fileops)
            return
        except OperationalError:
            if attempt == RECOVERY_ATTEMPTS:
                log.error("startup recovery could not commit: database stayed locked")
                raise
            delay = backoff(attempt, timedelta(seconds=5), timedelta(seconds=30))
            log.warning(
                "startup recovery attempt %d failed (locked); retrying in %s", attempt, delay
            )
            sleep(delay.total_seconds())


def _new_queue(
    services: Services,
) -> tuple[JobQueue, str, dict[JobKind, Handler], tuple[Callable[[], None], ...]]:
    settings, sessions, clock, fileops = (
        services.settings,
        services.session_factory,
        services.clock,
        services.fileops,
    )
    _recover(fileops, sessions)
    queue = JobQueue(services.engine, clock, fail_request_of_job)
    merger = services.merger
    acoustid = AcoustIdClient(PoliteClient("acoustid", settings, sessions), settings)
    pipeline = _pipeline(services, acoustid)  # the submitter reuses this one client
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
        acoustid,
        bool(settings.acoustid_app_key and settings.acoustid_user_key),
    )
    migrator = Migrator(
        sessions,
        clock,
        Path(settings.import_source_dir),
        Path(settings.config_dir),
        fileops.library,
        settings.mb_rate,
    )
    dropper = DropFolder(sessions, clock, settings.drop_dir, Path(settings.config_dir) / "drop")
    adopt = adopter(
        sessions,
        clock,
        Path(settings.downloads_dir),
        Path(settings.config_dir) / "adopt",
        settings.adopt_downloads,
        settings.adopt_settle_minutes,
    )
    watcher = Watcher(
        sessions,
        clock,
        services.musicbrainz,
        media=tuple(settings.canonical_media),
        countries=tuple(settings.canonical_countries),
    )
    backfill = NameBackfill(sessions, clock, fileops)
    purger = TrashPurger(sessions, clock, fileops, settings.trash_days)
    nightly = NightlyBackup(
        sessions,
        clock,
        services.engine,
        settings.backup_dir or Path(settings.config_dir) / "backups",
    )
    discovery = Discovery(
        sessions,
        clock,
        ListenBrainzClient(services.listenbrainz_http),
        lambda user, password: NavidromeUser(
            services.navidrome_http, settings.navidrome_url, user, password
        ),
        services.secret_box,
        main_library=settings.navidrome_library,
        discover_library=settings.navidrome_discover_library,
    )
    retention = Retention(
        discovery,
        pipeline.importer,
        fileops,
        pipeline.metadata,
        tuple(settings.canonical_media),
        tuple(settings.canonical_countries),
    )
    handlers = (
        HANDLERS
        | pipeline.handlers()
        | migrator.handlers()
        | dropper.handlers()
        | adopt.handlers()
        | merger.handlers()
        | rechecker.handlers()
        | submitter.handlers()
        | watcher.handlers()
        | purger.handlers()
        | backfill.handlers()
        | nightly.handlers()
        | discovery.handlers()
        | retention.handlers()
    )

    def discovery_startup() -> None:
        discovery.start_chains()  # creates the default playlists retention must scan
        retention.start_chains()

    startup = (
        rechecker.start_chains,
        watcher.start_chains,
        purger.start_chains,
        backfill.start_chains,
        nightly.start_chains,
        dropper.start_chains,
        adopt.start_chains,
        discovery_startup,
    )

    return queue, f"{socket.gethostname()}:{os.getpid()}", handlers, startup


async def serve_worker() -> None:
    queue, worker, handlers, startup = _new_queue(build_services(Settings()))
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await run_worker(queue, worker, stop, handlers, startup=startup)


async def serve_all() -> None:
    """Web and worker in one process. uvicorn owns SIGTERM/SIGINT; the worker follows it."""
    import uvicorn

    services = build_services(Settings())
    queue, worker, handlers, startup = _new_queue(services)
    stop = asyncio.Event()
    server = uvicorn.Server(
        uvicorn.Config(
            lambda: create_app(services=services),
            factory=True,
            host="0.0.0.0",
            port=8080,
            log_level=services.settings.log_level.lower(),
        )
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
