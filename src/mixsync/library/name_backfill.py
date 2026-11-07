"""Fill `tracks.album_artist/album/year` for rows imported before ADR 0018: a one-off job that
only reads tags (no network, no file write). A missing or unreadable file gets empty strings."""

import asyncio
import logging

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.pipeline import Handler
from mixsync.core.clock import Clock
from mixsync.core.jobs import LIVE, JobKind
from mixsync.db.models.library import Track
from mixsync.db.models.work import Job
from mixsync.db.queue import JobRecord, enqueue_in
from mixsync.library import tags
from mixsync.library.fileops import FileOps

log = logging.getLogger(__name__)
BATCH = 200


class NameBackfill:
    def __init__(self, sessions: sessionmaker[Session], clock: Clock, fileops: FileOps) -> None:
        self.sessions, self.clock, self.fileops = sessions, clock, fileops

    def handlers(self) -> dict[JobKind, Handler]:
        return {JobKind.NAME_BACKFILL: self.run}

    def start_chains(self) -> None:
        """On startup: queue the job if rows are unfilled and none is live."""
        with self.sessions.begin() as s:
            live = s.scalar(
                select(Job.id)
                .where(Job.kind == JobKind.NAME_BACKFILL, Job.status.in_(LIVE))
                .limit(1)
            )
            if live is None and s.scalar(select(Track.id).where(Track.album.is_(None)).limit(1)):
                enqueue_in(s, self.clock.now(), JobKind.NAME_BACKFILL, {})

    async def run(self, job: JobRecord) -> None:
        await asyncio.to_thread(self.fill)

    def fill(self) -> None:
        with self.sessions() as s:
            total = s.scalar(select(func.count()).where(Track.album.is_(None))) or 0
        done, last = 0, 0
        while True:
            with self.sessions() as s:
                rows = s.execute(
                    select(Track.id, Track.path)
                    .where(Track.album.is_(None), Track.id > last)
                    .order_by(Track.id)
                    .limit(BATCH)
                ).all()
            if not rows:
                return
            names = [(i, tags.file_names(self.fileops.library / p)) for i, p in rows]
            with self.sessions.begin() as s:
                for i, (aa, al, y) in names:  # a writer that got there first keeps its value
                    s.execute(
                        update(Track)
                        .where(Track.id == i, Track.album.is_(None))
                        .values(album_artist=aa, album=al, year=y)
                    )
            last, done = rows[-1][0], done + len(rows)
            log.info("name backfill: %d of %d tracks", done, total)
