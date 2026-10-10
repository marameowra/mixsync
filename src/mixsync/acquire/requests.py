"""Request state shared by the pipeline handlers and the web layer: creating a request,
marking candidates tried, coalescing rescans and applying a reviewer's decision."""

from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mixsync.core.jobs import JobKind, JobState
from mixsync.core.matching import Candidate, CandidateFile, Profile
from mixsync.db import decisions
from mixsync.db.models.auth import MatchingProfile, User
from mixsync.db.models.work import Job, Request
from mixsync.db.queue import enqueue_in
from mixsync.match.profiles import BALANCED

RESCAN_DELAY = timedelta(seconds=30)  # lets a burst of imports share one rescan
VERIFY_ATTEMPTS = 10  # AcoustID being down must not exhaust the retries in minutes


def candidate_from_dict(d: dict[str, Any]) -> Candidate:
    files = tuple(CandidateFile(**f) for f in d["files"])
    return Candidate(**{k: v for k, v in d.items() if k != "files"}, files=files)


def candidate_dict(c: Candidate) -> dict[str, Any]:
    return asdict(c)


def profile_for(s: Session, user_id: int) -> Profile:
    user = s.get(User, user_id)
    pid = user.matching_profile_id if user else None
    row = s.get(MatchingProfile, pid) if pid else None
    return Profile(row.name, row.auto_accept_max, row.review_max) if row else BALANCED


def create_request(s: Session, now: datetime, user_id: int, release_mbid: str) -> Request:
    req = Request(user_id=user_id, release_mbid=release_mbid)
    s.add(req)
    s.flush()
    enqueue_in(s, now, JobKind.SEARCH, {"request_id": req.id})
    return req


def retry_search(s: Session, now: datetime, req: Request, peer: str, folder: str, why: str) -> None:
    """The candidate is out (never deleted: its files stay in /downloads); look for the next."""
    req.tried = [*req.tried, [peer, folder]]
    req.status = "searching"
    req.last_error = why
    enqueue_in(s, now, JobKind.SEARCH, {"request_id": req.id})


def enqueue_rescan(s: Session, now: datetime) -> None:
    """At most one rescan waits in the queue; it will see every file imported before it runs."""
    waiting = s.scalar(
        select(Job.id).where(
            Job.kind == JobKind.RESCAN, Job.status.in_([JobState.QUEUED, JobState.RETRY_WAIT])
        )
    )
    if waiting is None:
        enqueue_in(s, now, JobKind.RESCAN, {}, run_after=now + RESCAN_DELAY)


def enqueue_import(s: Session, now: datetime, req: Request, decision_id: int) -> None:
    req.status = "importing"
    enqueue_in(
        s,
        now,
        JobKind.IMPORT,
        {"request_id": req.id, "decision_id": decision_id},
        idempotency_key=f"import:{req.id}:{decision_id}",
    )


def apply_review(s: Session, now: datetime, decision_id: int) -> None:
    """Carry out a reviewer's accept / repick / reject of a stage 2 decision."""
    d = decisions.get(s, decision_id)
    req = s.get(Request, d.evidence.get("request_id", 0)) if d else None
    if d is None or req is None:  # a decision that did not come from a request
        return
    src = d.evidence["source"]
    if d.final_action == "accept":
        enqueue_import(s, now, req, d.id)
    elif d.final_action == "repick":
        req.status = "verifying"
        enqueue_in(
            s,
            now,
            JobKind.VERIFY,
            {
                "request_id": req.id,
                "release_mbid": d.chosen_release_mbid,
                "candidate": d.evidence["candidate"],
                "paths": [f["local_path"] for f in src["files"]],
            },
            max_attempts=VERIFY_ATTEMPTS,
        )
    else:
        retry_search(s, now, req, src["peer"], src["folder"], "rejected in review")
