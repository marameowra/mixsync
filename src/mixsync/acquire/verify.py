"""Starting a VERIFY job, whatever the origin (download, migration, drop folder, repick,
re-check), and the batch id of the import that follows it.

The only place that builds or reads VERIFY payloads and `request-<id>-decision-<id>` batch ids.
Whether a request's release was given or identified is kept on the request (`release_given`),
so nothing has to read old jobs back to know. Nothing here commits: the job is added in the
caller's transaction, next to the state it belongs with (ADR 0004).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, String, and_, cast, literal, select
from sqlalchemy.orm import Session

from mixsync.acquire import lifecycle
from mixsync.core.jobs import LIVE, JobKind
from mixsync.db.models.matching import MatchDecision
from mixsync.db.models.work import Job, Request
from mixsync.db.queue import enqueue_in

VERIFY_ATTEMPTS = 10  # AcoustID being down must not exhaust the retries in minutes


def batch_id(request_id: int, decision_id: int) -> str:
    return f"request-{request_id}-decision-{decision_id}"


def decision_of(batch: str) -> int:
    return int(batch.rpartition("-")[2])


def in_request(column: Any, request_id: Any) -> ColumnElement[bool]:
    """`column` (a batch id) is one of the request's: a SQL condition; `request_id` may be a
    column to join on."""
    return column.like(literal("request-") + cast(request_id, String) + "-%")


def _of(request_id: int) -> ColumnElement[bool]:
    return and_(Job.kind == JobKind.VERIFY, Job.payload["request_id"].as_integer() == request_id)


def start(
    s: Session,
    now: datetime,
    req: Request,
    release_mbid: str | None,
    candidate: dict[str, Any],
    paths: list[str],
    *,
    priority: int = 0,
    idempotency_key: str | None = None,
) -> None:
    """Verify `paths` against `release_mbid`, or None to have the release identified."""
    lifecycle.verifying(req)
    req.release_given = bool(release_mbid)
    enqueue_in(
        s,
        now,
        JobKind.VERIFY,
        {
            "request_id": req.id,
            "release_mbid": release_mbid,
            "candidate": candidate,
            "paths": paths,
        },
        max_attempts=VERIFY_ATTEMPTS,
        priority=priority,
        idempotency_key=idempotency_key,
    )


def again(
    s: Session, now: datetime, req: Request, release_mbid: str | None, evidence: dict[str, Any]
) -> None:
    """Verify the files a decision was made on again, against `release_mbid` (a repick, or a
    release that changed under a stored assignment)."""
    start(
        s,
        now,
        req,
        release_mbid,
        evidence["candidate"],
        [f["local_path"] for f in evidence["source"]["files"]],
    )


def recheck(s: Session, now: datetime, req: Request, d: MatchDecision) -> None:
    """Verify again with the current scorer. A request whose release was given keeps the one
    the decision recorded; one that was identified is identified again."""
    given = (d.release_mbid or req.release_mbid) if req.release_given else None
    again(s, now, req, given, d.evidence)


def rematch(s: Session, now: datetime, req: Request, priority: int) -> bool:
    """Run the request's first VERIFY again, its release to be identified. False if it never
    had one."""
    first = s.scalar(select(Job).where(_of(req.id)).order_by(Job.id).limit(1))
    if first is None:
        return False
    start(s, now, req, None, first.payload["candidate"], first.payload["paths"], priority=priority)
    return True


def started(s: Session, req_id: int) -> bool:
    return s.scalar(select(Job.id).where(_of(req_id)).limit(1)) is not None


def running(s: Session, req_id: int) -> bool:
    live = select(Job.id).where(_of(req_id), Job.status.in_(LIVE))
    return s.scalar(live.limit(1)) is not None


def superseded(s: Session, job_id: int, req_id: int) -> bool:
    """A later VERIFY of the request (a repick or re-check) replaced this one."""
    later = select(Job.id).where(_of(req_id), Job.id > job_id)
    return s.scalar(later.limit(1)) is not None


def first_of(s: Session, ids: set[int]) -> dict[int, tuple[str, int]]:
    """(folder, file count) of the first VERIFY of each request."""
    out: dict[int, tuple[str, int]] = {}
    for j in s.scalars(select(Job).where(Job.kind == JobKind.VERIFY).order_by(Job.id)):
        p = j.payload
        if p["request_id"] in ids:
            folder = p.get("candidate", {}).get("folder", "")
            out.setdefault(p["request_id"], (folder, len(p.get("paths", []))))
    return out


def requests_of_key(s: Session, key_prefix: str) -> list[int]:
    """The requests of the VERIFY jobs whose idempotency key starts with `key_prefix`, oldest
    first (a drop folder retries a failed album under a numbered key)."""
    return [
        j.payload["request_id"]
        for j in s.scalars(
            select(Job)
            .where(Job.kind == JobKind.VERIFY, Job.idempotency_key.like(f"{key_prefix}%"))
            .order_by(Job.id)
        )
    ]
