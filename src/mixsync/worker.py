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
from mixsync.acquire.adopter import adopter
from mixsync.acquire.dropfolder import DropFolder
from mixsync.acquire.gate import DownloadGate
from mixsync.acquire.merge import Merger
from mixsync.acquire.migration import Migrator
from mixsync.acquire.pipeline import Pipeline
from mixsync.acquire.recheck import Rechecker
from mixsync.acquire.requests import fail_request_of_job
from mixsync.core.clock import Clock, SystemClock
from mixsync.core.config import Settings
from mixsync.core.errors import TransientError
from mixsync.core.jobs import DEFAULT_LEASE, JobKind
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.journal import SqlJournal
from mixsync.db.queue import JobQueue, JobRecord, LeaseLostError
from mixsync.db.secretbox import load_box
from mixsync.discovery.listenbrainz import ListenBrainzClient
from mixsync.discovery.playlists import Discovery
from mixsync.discovery.retention import Retention
from mixsync.library import tag_undo
from mixsync.library.fileops import FileOps
from mixsync.library.importer import Importer
from mixsync.library.purge import TrashPurger
from mixsync.metadata.acoustid import AcoustIdClient, fingerprint
from mixsync.metadata.coverart import CoverArtClient
from mixsync.metadata.musicbrainz import MusicBrainzProvider
from mixsync.ratelimit.client import PoliteClient
from mixsync.sources.query import queries
from mixsync.sources.slskd import SlskdSource
from mixsync.targets.navidrome import NavidromeTarget, NavidromeUser
from mixsync.watchlist.poller import Watcher

log = logging.getLogger(__name__)

COMPLETE_TRIES = 4
COMPLETE_PAUSE = 0.2  # seconds, times the attempt number

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


async def _complete(queue: JobQueue, job_id: int, worker: str) -> None:
    """The work is done: a locked database must never send it to retry (it would run twice)."""
    for attempt in range(COMPLETE_TRIES):
        try:
            queue.complete(job_id, worker)
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
        queue.start(job.id, worker)
        await handlers[job.kind](job)
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
    else:
        await _complete(queue, job.id, worker)
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
    retry_at = loop.time()
    # shortcut: queue calls are sync and short, run on the loop; move to threads if they block it.
    while not stop.is_set():
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
        return PoliteClient(service, settings, sessions)

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
        metadata=MusicBrainzProvider(client("musicbrainz"), settings.mb_base_url),
        fingerprint=fingerprint,
        acoustid=AcoustIdClient(client("acoustid"), settings),
        importer=Importer(fileops, sessions, clock, settings.path_template),
        reencode=True,
        flac_level=settings.flac_level,
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


def _new_queue() -> tuple[JobQueue, str, dict[JobKind, Handler], tuple[Callable[[], None], ...]]:
    settings = Settings()
    load_box(settings)  # refuse a bad key file at startup (serve_all too), not at first use
    engine = make_engine(settings)
    clock = SystemClock()
    sessions = make_session_factory(engine)
    fileops = FileOps(
        Path(settings.data_dir),
        Path(settings.downloads_dir),
        SqlJournal(sessions, clock),
        clock,
        settings.drop_dir,
    )
    fileops.recover()
    tag_undo.recover(sessions, fileops)
    queue = JobQueue(engine, clock, fail_request_of_job)
    merger = Merger(
        sessions,
        clock,
        MusicBrainzProvider(PoliteClient("musicbrainz", settings, sessions), settings.mb_base_url),
        fileops,
        settings.path_template,
        tuple(settings.canonical_media),
        tuple(settings.canonical_countries),
        settings.trash_days,
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
        AcoustIdClient(PoliteClient("acoustid", settings, sessions), settings),
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
        MusicBrainzProvider(PoliteClient("musicbrainz", settings, sessions), settings.mb_base_url),
        media=tuple(settings.canonical_media),
        countries=tuple(settings.canonical_countries),
    )
    purger = TrashPurger(sessions, clock, fileops, settings.trash_days)
    navidrome_http = PoliteClient("navidrome", settings, sessions)
    discovery = Discovery(
        sessions,
        clock,
        ListenBrainzClient(PoliteClient("listenbrainz", settings, sessions)),
        lambda user, password: NavidromeUser(
            navidrome_http, settings.navidrome_url, user, password
        ),
        load_box(settings),
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
        dropper.start_chains,
        adopt.start_chains,
        discovery_startup,
    )

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
