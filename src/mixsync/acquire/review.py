"""A reviewer's (or the owner's) action on a stage 2 decision: each operation claims the
decision and moves its request on in the caller's transaction, so there is no second call to
forget. Every action records who acted and when (ADR 0005). `split` is the exception: it
leaves the decision pending, so it claims nothing and records no actor."""

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from mixsync.acquire import lifecycle, verify
from mixsync.acquire.evidence import Evidence
from mixsync.acquire.requests import drop_candidate, enqueue_import
from mixsync.core.jobs import JobKind
from mixsync.db import decisions
from mixsync.db.models.matching import MatchDecision
from mixsync.db.models.work import Request
from mixsync.db.queue import enqueue_in

RecheckOutcome = Literal["queued", "missing", "stale", "busy"]


class Refused(Exception):
    """The action cannot be taken now (decision already acted on, nothing to import, ...)."""


def _claim(
    s: Session, decision_id: int, actor_id: int, action: str, mbid: str | None = None
) -> tuple[MatchDecision, Evidence, Request | None]:
    try:
        decisions.act(s, decision_id, actor_id, action, mbid)
    except decisions.DecisionStateError as e:
        raise Refused(str(e)) from e
    d = s.get_one(MatchDecision, decision_id)
    ev = Evidence.parse(d.evidence)
    return d, ev, s.get(Request, ev.request_id)  # None: the decision did not come from a request


def _files_known(d: MatchDecision | None) -> bool:
    files = Evidence.parse(d.evidence).files if d else []
    return bool(files) and all(f.get("local_path") for f in files)


def accept(s: Session, now: datetime, decision_id: int, actor_id: int) -> None:
    d = s.get(MatchDecision, decision_id)
    if d is not None and not d.release_mbid:  # nothing matched: nothing to import
        raise Refused("no release was found; pick one or mark unverified")
    d, _, req = _claim(s, decision_id, actor_id, "accept")
    if req:
        enqueue_import(s, now, req, d.id)


def reject(s: Session, now: datetime, decision_id: int, actor_id: int) -> None:
    _, ev, req = _claim(s, decision_id, actor_id, "reject")
    if _files_known(s.get(MatchDecision, decision_id)):  # the job moves any from quarantine/review
        enqueue_in(
            s,
            now,
            JobKind.REFILE,
            {"decision_id": decision_id},
            idempotency_key=f"refile:{decision_id}",
        )
    if req is None:
        return
    if req.from_files:  # no other candidate to look for: the album stays out
        lifecycle.rejected(req)
    else:
        drop_candidate(s, now, req, ev.peer, ev.folder, "rejected in review", spent=True)


def repick(s: Session, now: datetime, decision_id: int, actor_id: int, release_mbid: str) -> None:
    d, ev, req = _claim(s, decision_id, actor_id, "repick", release_mbid)
    if req:
        verify.again(s, now, req, d.chosen_release_mbid, ev)


def split(
    s: Session, now: datetime, decision_id: int, paths: list[str], release_mbid: str
) -> Request:
    """Move some unassigned files of a pending decision to a new from-files request that
    verifies them against `release_mbid`. The decision stays pending for the rest and is not
    scored again. No file moves here: the new request's VERIFY and IMPORT handle them."""
    d = s.get(MatchDecision, decision_id, with_for_update=True)  # two splits of one file
    if d is None or d.status != decisions.PENDING or d.stage != 2:
        raise Refused(f"decision {decision_id} is not pending review")
    ev = Evidence.parse(d.evidence)
    parent = s.get(Request, ev.request_id)
    if parent is not None and parent.provisional:
        raise Refused("discovery reviews cannot be split")
    if not paths:
        raise Refused("pick the files to split off")
    if stray := [p for p in paths if p not in ev.unassigned]:
        raise Refused(f"not an unassigned file of this review: {Path(stray[0]).name}")
    try:
        mbid = str(uuid.UUID(release_mbid))
    except ValueError:
        raise Refused("not a MusicBrainz release MBID") from None
    if gone := [p for p in paths if not Path(p).exists()]:
        raise Refused(f"file no longer exists: {Path(gone[0]).name}")
    paths = list(dict.fromkeys(paths))
    req = Request(
        user_id=d.user_id, release_mbid=mbid, artist=parent.artist if parent else "", source="split"
    )
    s.add(req)
    s.flush()
    verify.start(s, now, req, mbid, ev.candidate_of(paths), paths)
    d.evidence = ev.with_split(paths, req.id).to_json()
    return req


def unverified(
    s: Session, now: datetime, decision_id: int, actor_id: int, edits: dict[str, Any]
) -> None:
    if not _files_known(s.get(MatchDecision, decision_id)):
        raise Refused("the downloaded files are not known")
    d, ev, req = _claim(s, decision_id, actor_id, "unverified")
    d.evidence = ev.with_unverified(edits).to_json()
    if req:
        enqueue_import(s, now, req, d.id)


def recheck(s: Session, now: datetime, decision_id: int, actor_id: int) -> RecheckOutcome:
    """Close the decision and verify its files again with the current scorer."""
    d = s.get(MatchDecision, decision_id)
    req = s.get(Request, Evidence.parse(d.evidence).request_id) if d else None
    if d is None or req is None:  # not from a request: nothing to verify again
        return "missing"
    if req.status != "review":  # already importing, imported or searching again
        return "stale"
    if not _files_known(d) or not all(
        Path(f["local_path"]).exists() for f in Evidence.parse(d.evidence).files
    ):
        return "missing"
    if verify.running(s, req.id):
        return "busy"
    try:
        _claim(s, decision_id, actor_id, "recheck")
    except Refused:  # acted on since the list was read
        return "busy"
    verify.recheck(s, now, req, s.get_one(MatchDecision, decision_id))
    return "queued"


def cancel(s: Session, request_id: int, actor_id: int) -> None:
    """The request is going away: its pending reviews close as cancelled, not as a rejection
    nobody made. Refused if a review acts on one of them first."""
    pending = select(MatchDecision).where(
        MatchDecision.status == decisions.PENDING, MatchDecision.stage == 2
    )
    for d in list(s.scalars(pending)):
        if Evidence.parse(d.evidence).request_id == request_id:
            try:
                decisions.act(s, d.id, actor_id, "cancel")
            except decisions.DecisionStateError:
                raise Refused("a review is acting on this request") from None
