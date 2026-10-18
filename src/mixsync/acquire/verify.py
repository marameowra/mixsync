"""Starting a VERIFY job, whatever the origin (download, migration, drop folder, repick,
re-check), and the batch id of the import that follows it.

The only place that builds or reads VERIFY payloads and `request-<id>-decision-<id>` batch ids.
Whether a decision's release was given or identified is kept on the decision (`release_given`),
so nothing has to read old jobs back to know. Nothing here commits: the job is added in the
caller's transaction, next to the state it belongs with (ADR 0004).
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, String, and_, cast, literal, select
from sqlalchemy.orm import Session

from mixsync.acquire import lifecycle
from mixsync.acquire.evidence import Evidence
from mixsync.core.errors import PermanentError
from mixsync.core.jobs import LIVE, JobKind, backoff
from mixsync.db.models.matching import MatchDecision
from mixsync.db.models.work import Job, Request
from mixsync.db.queue import JobRecord, enqueue_in

VERIFY_ATTEMPTS = 10  # AcoustID being down must not exhaust the retries in minutes
ACOUSTID_PATIENCE = timedelta(hours=24)  # from the first failed lookup


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


def wait_for_acoustid(s: Session, now: datetime, req: Request, job: JobRecord) -> None:
    """AcoustID is unreachable: verify again later, backing off, for up to ACOUSTID_PATIENCE from
    the first failure. Then the error is permanent, which fails the request."""
    since = datetime.fromisoformat(job.payload.get("acoustid_since", now.isoformat()))
    if now - since > ACOUSTID_PATIENCE:
        raise PermanentError("AcoustID unreachable for 24 hours")
    tries = job.payload.get("acoustid_tries", 0) + 1
    req.last_error = "waiting for AcoustID"
    enqueue_in(
        s,
        now,
        JobKind.VERIFY,
        {**job.payload, "acoustid_since": since.isoformat(), "acoustid_tries": tries},
        run_after=now + backoff(tries),
        max_attempts=VERIFY_ATTEMPTS,
        priority=s.scalar(select(Job.priority).where(Job.id == job.id)) or 0,
        idempotency_key=f"verify:acoustid:{job.id}",
    )


def again(
    s: Session, now: datetime, req: Request, release_mbid: str | None, evidence: Evidence
) -> None:
    """Verify the files a decision was made on again, against `release_mbid` (a repick, or a
    release that changed under a stored assignment)."""
    start(s, now, req, release_mbid, evidence.candidate, evidence.paths)


def recheck(s: Session, now: datetime, req: Request, d: MatchDecision) -> None:
    """Verify again with the current scorer. A decision whose release was given keeps the one
    it recorded; one that was identified is identified again."""
    given = (d.release_mbid or req.release_mbid) if d.release_given else None
    again(s, now, req, given, Evidence.parse(d.evidence))


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
