"""Request state shared by the pipeline handlers and the web layer: creating a request,
marking candidates tried, coalescing rescans and applying a reviewer's decision."""

import random
from collections.abc import Iterable
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mixsync.core.jobs import JobKind, JobState
from mixsync.core.matching import Candidate, CandidateFile, Profile
from mixsync.db import decisions
from mixsync.db.models.auth import MatchingProfile, User
from mixsync.db.models.discovery import DiscoveryPlaylist, ProvisionalHold, ProvisionalTrack
from mixsync.db.models.library import Track
from mixsync.db.models.work import Job, Request
from mixsync.db.queue import enqueue_in
from mixsync.library import tags
from mixsync.match.profiles import BALANCED

RESCAN_DELAY = timedelta(seconds=30)  # lets a burst of imports share one rescan
RECHECK_INTERVALS = (timedelta(weeks=1), timedelta(weeks=2), timedelta(days=30), timedelta(days=91))
LIVE = (JobState.QUEUED, JobState.LEASED, JobState.RUNNING, JobState.RETRY_WAIT)
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
            enqueue_in(
                s,
                now,
                JobKind.DISCOVERY_PUSH,
                {"playlist_id": pl.id},
                run_after=now + 2 * RESCAN_DELAY,
                idempotency_key=f"discovery-push:{pl.id}:import:{batch}",
            )


def retry_search(s: Session, now: datetime, req: Request, peer: str, folder: str, why: str) -> None:
    """The candidate is out (never deleted: its files stay in /downloads); look for the next."""
    req.tried = [*req.tried, [peer, folder]]
    req.status = "searching"
    req.last_error = why
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
    if d.final_action in ("accept", "unverified"):  # unverified: the edits are in the evidence
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
    elif req.migration_id is not None:  # no other candidate to look for: the album stays out
        req.status, req.last_error = "skipped", "rejected in review"
    else:
        retry_search(s, now, req, src["peer"], src["folder"], "rejected in review")


AlbumKey = tuple[str, str, str]  # (artist, album, year) as the unverified list groups them


def year_of(tg: dict[str, str]) -> str:
    y = tg.get("date", "")[:4]
    return y if y.isascii() and y.isdigit() and y != "0000" else ""


def album_key(tg: dict[str, str]) -> AlbumKey:
    return tg.get("albumartist") or tg.get("artist", ""), tg.get("album", ""), year_of(tg)


def live_rechecks(s: Session, key: AlbumKey) -> list[Job]:
    return [
        j
        for j in s.scalars(select(Job).where(Job.kind == JobKind.RECHECK, Job.status.in_(LIVE)))
        if tuple(j.payload["album"]) == key
    ]


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
    delay = RECHECK_INTERVALS[min(attempt, len(RECHECK_INTERVALS) - 1)]
    enqueue_in(
        s,
        now,
        JobKind.RECHECK,
        {"album": list(key), "user_id": user_id, "attempt": attempt},
        run_after=now + delay * random.uniform(0.5, 1.0) if background else now,
        idempotency_key=chain_key,
    )


Item = tuple[Track, dict[str, str]]


def read_unverified(db: Session, root: Path) -> list[Item]:
    # shortcut: reads the tags of every unverified file per call; move the album fields
    # into DB columns when unverified sets get large.
    rows = db.scalars(select(Track).where(Track.status == "unverified").order_by(Track.id)).all()
    items: list[Item] = []
    for t in rows:
        try:
            items.append((t, tags.read_fields(root / t.path)))
        except Exception:  # file moved or unreadable: the path alone still identifies it
            items.append((t, {}))
    return items


def group(items: list[Item]) -> dict[AlbumKey, list[Item]]:
    """Group by tags, not import batch (a batch can mix albums). A year-less group joins the one
    dated group with the same artist and album, if there is exactly one."""
    albums: dict[AlbumKey, list[Item]] = {}
    for it in items:
        albums.setdefault(album_key(it[1]), []).append(it)
    for key in [k for k in albums if not k[2]]:
        dated = [k for k in albums if k[:2] == key[:2] and k[2]]
        if len(dated) == 1:
            albums[dated[0]] += albums.pop(key)
    return albums
