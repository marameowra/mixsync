"""The admin scan for done requests whose release still lacks tracks in the library (#370).

The MISSING_SCAN job is the dry run: it changes no request and stores the ones with gaps as a
draft `Plan` of kind KIND. Confirm reopens each listed request that is unchanged since as
`partial` and queues its search; discard drops the draft. Neither touches a file or the network.
"""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire import lifecycle
from mixsync.acquire.pipeline import Handler
from mixsync.acquire.requests import search_priority
from mixsync.core.clock import Clock
from mixsync.core.errors import PermanentError
from mixsync.core.jobs import LIVE, JobKind
from mixsync.core.protocols import MetadataProvider
from mixsync.db import canonical as canonical_db
from mixsync.db.models.library import Plan
from mixsync.db.models.work import Job, Request
from mixsync.db.queue import JobRecord, enqueue_in

log = logging.getLogger(__name__)
KIND = "missing_tracks"  # Plan.kind of the draft: no file op, never undoable, blocks nothing
NOTE = "found by the missing-track scan"


def enqueue_scan(s: Session, now: datetime) -> None:
    """Queue the scan unless one is live. The payload holds the newest plan id so far: the scan's
    draft, if it makes one, is the first KIND plan after it."""
    live = select(Job.id).where(Job.kind == JobKind.MISSING_SCAN, Job.status.in_(LIVE))
    if s.scalar(live.limit(1)) is None:
        after = s.scalar(select(func.max(Plan.id))) or 0
        enqueue_in(s, now, JobKind.MISSING_SCAN, {"after": after})


def latest(s: Session) -> tuple[Job | None, Plan | None]:
    """The newest scan job and the draft it made, if it made one."""
    job = s.scalars(
        select(Job).where(Job.kind == JobKind.MISSING_SCAN).order_by(Job.id.desc())
    ).first()
    if job is None:
        return None, None
    made = select(Plan).where(Plan.kind == KIND, Plan.id > job.payload.get("after", 0))
    return job, s.scalars(made.order_by(Plan.id)).first()


def confirm(s: Session, now: datetime, plan: Plan) -> int:
    """Reopen every listed request whose status is still the scanned one; returns how many."""
    reopened = 0
    for op in plan.ops:
        req = s.get(Request, op["request_id"])
        if req is None or req.status != op["status"]:
            continue  # gone or moved on since the scan: left alone
        lifecycle.keep_looking(req, op["note"], [NOTE])
        enqueue_in(s, now, JobKind.SEARCH, {"request_id": req.id}, priority=search_priority(req))
        reopened += 1
    plan.status = "done"
    return reopened


def _deleted(s: Session) -> dict[str, set[str]]:
    """Release group -> recordings a done library delete removed on purpose (an undone one is
    `undone`, so its recordings count as missing again)."""
    gone: dict[str, set[str]] = {}
    for p in s.scalars(select(Plan).where(Plan.kind == "delete", Plan.status == "done")):
        for op in p.ops:
            if op["op"] != "trash":
                continue
            group: str | None = op["old"]["release_group_mbid"]
            recording: str | None = op["old"]["recording_mbid"]
            if group and recording:
                gone.setdefault(group, set()).add(recording)
    return gone


class MissingScan:
    def __init__(
        self, sessions: sessionmaker[Session], clock: Clock, metadata: MetadataProvider
    ) -> None:
        self.sessions, self.clock, self.metadata = sessions, clock, metadata

    def handlers(self) -> dict[JobKind, Handler]:
        return {JobKind.MISSING_SCAN: self.run}

    async def run(self, job: JobRecord) -> None:
        with self.sessions() as s:
            done = s.scalars(
                select(Request)
                .where(Request.status.in_(("imported", "owned")), Request.provisional.is_(False))
                .order_by(Request.id)
            )
            todo = [(r.id, r.release_mbid, r.status) for r in done if not r.from_files]
            gone = _deleted(s)
        ops: list[dict[str, Any]] = []
        for rid, mbid, status in todo:  # one release at a time: the provider is rate-limited
            try:
                album = await self.metadata.get_release(mbid)  # a TransientError retries the job
            except PermanentError as e:  # e.g. a release MusicBrainz removed: not checkable
                log.warning("missing-track scan: request %d not checked: %s", rid, e)
                continue
            group = album.release_group_id
            if not album.tracks or not group:
                continue
            recordings = [t.recording_id or "" for t in album.tracks]
            with self.sessions() as s:
                have = canonical_db.owned(s, group, recordings)
            skip = have | gone.get(group, set())  # deleted on purpose: not missing
            missing = [t.title for t in album.tracks if t.recording_id not in skip]
            if missing:
                present, total = sum(r in have for r in recordings), len(recordings)
                ops.append(
                    {
                        "op": "reopen",
                        "request_id": rid,
                        "artist": album.artist,
                        "album": album.album,
                        "status": status,
                        "note": f"{present} of {total} tracks present",
                        "missing": missing,
                    }
                )
        log.info("missing-track scan: %d of %d requests lack tracks", len(ops), len(todo))
        with self.sessions.begin() as s:
            for p in s.scalars(select(Plan).where(Plan.kind == KIND, Plan.status == "draft")):
                p.status = "discarded"  # an older preview: DB only
            if ops:  # nothing missing: no draft, the page says so
                s.add(Plan(kind=KIND, release_group_mbid="", canonical_release_mbid="", ops=ops))
