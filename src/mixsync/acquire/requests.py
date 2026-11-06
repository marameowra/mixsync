"""Request state shared by the pipeline handlers and the web layer: creating a request,
marking candidates tried, and coalescing rescans."""

import random
from collections.abc import Iterable, Sequence
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, select
from sqlalchemy.orm import Session

from mixsync.acquire import lifecycle, verify
from mixsync.acquire.evidence import Evidence
from mixsync.core.jobs import LIVE, JobKind, JobState, backoff
from mixsync.core.matching import Candidate, CandidateFile, Profile
from mixsync.db.models.auth import AppSettings, MatchingProfile, User
from mixsync.db.models.discovery import DiscoveryPlaylist, ProvisionalHold, ProvisionalTrack
from mixsync.db.models.library import Track
from mixsync.db.models.matching import MatchDecision
from mixsync.db.models.work import Job, Request
from mixsync.db.queue import enqueue_in
from mixsync.library import albums
from mixsync.match.profiles import BALANCED, from_confidence

REQUEST_BUDGET = 5  # dropped candidates (after bytes landed) a request may spend before it fails

RESCAN_DELAY = timedelta(seconds=30)  # lets a burst of imports share one rescan
LATE_PUSH = timedelta(minutes=10)
RECHECK_INTERVALS = (timedelta(weeks=1), timedelta(weeks=2), timedelta(days=30), timedelta(days=91))


def no_results_delay(attempts: int) -> timedelta:
    """Wait before the next search after `attempts` (>= 1) in a row found nothing."""
    return backoff(attempts, timedelta(hours=1), timedelta(weeks=1))


def candidate_from_dict(d: dict[str, Any]) -> Candidate:
    files = tuple(CandidateFile(**f) for f in d["files"])
    return Candidate(**{k: v for k, v in d.items() if k != "files"}, files=files)


def candidate_dict(c: Candidate) -> dict[str, Any]:
    return asdict(c)


def auto_accept_cap(s: Session) -> float | None:
    """The admin's minimum auto-accept confidence as a distance; None when there is no cap."""
    row = s.get(AppSettings, 1)
    pct = row.min_auto_accept_confidence if row else None
    return None if pct is None else from_confidence(pct)


def profile_for(s: Session, user_id: int | None) -> Profile:
    """The user's profile (or BALANCED without one), its auto-accept limit held to the cap."""
    user = s.get(User, user_id) if user_id else None
    pid = user.matching_profile_id if user else None
    row = s.get(MatchingProfile, pid) if pid else None
    p = Profile(row.name, row.auto_accept_max, row.review_max) if row else BALANCED
    cap = auto_accept_cap(s)
    return p if cap is None or p.auto_accept_max <= cap else replace(p, auto_accept_max=cap)


def search_priority(req: Request) -> int:
    """Lower runs first: discovery searches wait behind manual and watchlist ones."""
    return 1 if req.provisional else 0


def create_request(
    s: Session,
    now: datetime,
    user_id: int,
    release_mbid: str,
    source: str = "manual",
    release_group_mbid: str | None = None,
    provisional: bool = False,
    recordings: Iterable[str] = (),
) -> Request:
    recordings = list(recordings)
    if provisional and not recordings:
        raise ValueError("a provisional request needs at least one wanted recording")
    req = Request(
        user_id=user_id,
        release_mbid=release_mbid,
        source=source,
        release_group_mbid=release_group_mbid,
        provisional=provisional,
        recording_mbids=recordings,
    )
    s.add(req)
    s.flush()
    enqueue_in(s, now, JobKind.SEARCH, {"request_id": req.id}, priority=search_priority(req))
    return req


def hold_imports(s: Session, now: datetime, batch: str) -> None:
    """A discover import landed: start the holds on its tracks' retention clock and queue a push
    of each playlist that wants them (after the rescan that makes Navidrome see the files)."""
    for pt in s.scalars(
        select(ProvisionalTrack).where(
            ProvisionalTrack.batch_id == batch, ProvisionalTrack.status == "active"
        )
    ):
        for hold in s.scalars(
            select(ProvisionalHold).where(ProvisionalHold.recording_mbid == pt.recording_mbid)
        ):
            pl = s.get(DiscoveryPlaylist, hold.playlist_id)
            if pl is None:
                continue
            hold.provisional_track_id = hold.provisional_track_id or pt.id
            if hold.expires_at is None:
                hold.expires_at = now + timedelta(days=pl.retention_days)
            # a second, later push for when Navidrome's scan was still running at the first
            for suffix, delay in (("", 2 * RESCAN_DELAY), (":late", LATE_PUSH)):
                enqueue_in(
                    s,
                    now,
                    JobKind.DISCOVERY_PUSH,
                    {"playlist_id": pl.id},
                    run_after=now + delay,
                    idempotency_key=f"discovery-push:{pl.id}:import:{batch}{suffix}",
                )


def _has_tracks(s: Session, req: Request) -> bool:
    return (
        s.scalar(select(Track.id).where(verify.in_request(Track.batch_id, req.id)).limit(1))
        is not None
    )


def _failed_candidate(s: Session, payload: dict[str, Any]) -> list[str] | None:
    """[peer, folder] of a DOWNLOAD / VERIFY job's candidate, or of an IMPORT job's decision."""
    if "candidate" in payload:
        c = payload["candidate"]
        return [c["username"], c["folder"]]
    d = s.get(MatchDecision, payload["decision_id"]) if "decision_id" in payload else None
    if d is None:
        return None
    ev = Evidence.parse(d.evidence)
    return [ev.peer, ev.folder]


def fail_request_of_job(
    c: Connection, job_id: int, payload: dict[str, Any], error: str, now: datetime
) -> None:
    """The queue's final-failure hook: a job that failed for good must not leave its request in
    progress forever, unless a newer job for the request has taken over."""
    rid = payload.get("request_id")
    if rid is None:
        return
    with Session(c) as s:  # joins the queue's transaction: the caller commits
        req = s.get(Request, rid)
        newer = s.scalar(
            select(Job.id)
            .where(Job.id > job_id, Job.payload["request_id"].as_integer() == rid)
            .limit(1)
        )
        if req and req.status in lifecycle.IN_PROGRESS and newer is None:
            lifecycle.job_failed(req, _has_tracks(s, req), error)
            req.updated_at = now  # the queue's clock, as the job rows use
            if req.status == "partial":  # a partial request always has a repeat search queued
                peer, folder = _failed_candidate(s, payload) or ("", "")
                drop_candidate(s, now, req, peer, folder, error, job_failed=True)
            s.flush()


def drop_candidate(
    s: Session,
    now: datetime,
    req: Request,
    peer: str,
    folder: str,
    why: str,
    *,
    partial: str = "",  # "N of M tracks present": some tracks are in, keep looking with backoff
    notes: Sequence[str] = (),
    job_failed: bool = False,  # the queue gave up on a job; lifecycle already set
    spent: bool = False,  # its first bytes landed: uses one unit of the request budget
) -> None:
    """The one way a candidate is dropped: it joins the request's tried list (verify has already
    moved its files into quarantine, never deleted) and the request searches again, or fails
    when it has nothing to search for. The next search is fresh and skips every tried
    [peer, folder]."""
    if req.from_files:  # nothing to search for: a person must look at the files
        if not partial and not job_failed:
            lifecycle.download_failed(req, False, why)
        return
    if (
        peer and [peer, folder] not in req.tried
    ):  # no candidate known, or else the search re-picks it, hourly, forever
        req.tried = [*req.tried, [peer, folder]]
    if spent:  # a job_failed job is the queue's fault, not the candidate's: never spends
        req.dropped += 1
        if req.dropped >= REQUEST_BUDGET:
            lifecycle.failed(
                req, f"gave up after {REQUEST_BUDGET} dropped candidates (request budget): {why}"
            )
            return
    if partial or job_failed:  # part of the album is in: repeat the search with backoff
        if partial:
            lifecycle.keep_looking(req, partial, notes)
        req.no_results_attempts += 1
        delay = no_results_delay(req.no_results_attempts)
        req.next_search_at = now + delay
        enqueue_in(
            s,
            now,
            JobKind.SEARCH,
            {"request_id": req.id},
            run_after=now + delay,
            priority=search_priority(req),
        )
        return
    lifecycle.download_failed(req, _has_tracks(s, req), why)
    enqueue_in(s, now, JobKind.SEARCH, {"request_id": req.id}, priority=search_priority(req))


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
    lifecycle.importing(req)
    enqueue_in(
        s,
        now,
        JobKind.IMPORT,
        {"request_id": req.id, "decision_id": decision_id},
        idempotency_key=f"import:{req.id}:{decision_id}",
    )


AlbumKey = tuple[str, str, str]  # (album artist, album, year) as the unverified list groups them


def all_live_rechecks(s: Session) -> list[Job]:
    return list(s.scalars(select(Job).where(Job.kind == JobKind.RECHECK, Job.status.in_(LIVE))))


def live_rechecks(s: Session, key: AlbumKey) -> list[Job]:
    return [j for j in all_live_rechecks(s) if tuple(j.payload["album"]) == key]


def enqueue_recheck(
    s: Session,
    now: datetime,
    key: AlbumKey,
    user_id: int | None,
    *,
    attempt: int = 0,
    chain_key: str | None = None,
) -> None:
    """A background check (no user) waits a random point in the second half of its interval, so
    never under half of it and never at a fixed time; a user's runs now. Without a `chain_key`
    (the continuation of a check), nothing is added while a check of the same kind is waiting
    or running for the album."""
    background = user_id is None
    if not chain_key and any(
        (j.payload["user_id"] is None) == background for j in live_rechecks(s, key)
    ):
        return
    add_recheck(s, now, key, user_id, attempt=attempt, chain_key=chain_key)


def add_recheck(
    s: Session,
    now: datetime,
    key: AlbumKey,
    user_id: int | None,
    *,
    attempt: int = 0,
    chain_key: str | None = None,
) -> None:
    background = user_id is None
    delay = RECHECK_INTERVALS[min(attempt, len(RECHECK_INTERVALS) - 1)]
    enqueue_in(
        s,
        now,
        JobKind.RECHECK,
        {"album": list(key), "user_id": user_id, "attempt": attempt},
        run_after=now + delay * random.uniform(0.5, 1.0) if background else now,
        idempotency_key=chain_key,
    )


def read_unverified(db: Session) -> list[Track]:
    """Unverified tracks whose names are filled in (ADR 0018): no file is read."""
    return list(
        db.scalars(
            select(Track)
            .where(Track.status == "unverified", Track.album.is_not(None))
            .order_by(Track.id)
        )
    )


def group(tracks: list[Track]) -> dict[AlbumKey, list[Track]]:
    """Group by the name columns, not import batch (a batch can mix albums)."""
    raw: dict[AlbumKey, list[Track]] = {}
    for t in tracks:
        raw.setdefault((t.album_artist or "", t.album or "", t.year or ""), []).append(t)
    into = albums.fold(raw)
    out: dict[AlbumKey, list[Track]] = {}
    for key, ts in raw.items():
        out.setdefault(into[key], []).extend(ts)
    return out
