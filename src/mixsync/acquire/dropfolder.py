"""The drop folder: album folders put into MIXSYNC_DROP_DIR are picked up by a polling job.

Each scan reuses the migration scan to hash and group the folder, then makes one request and one
VERIFY job per album, so the normal pipeline identifies, imports (originals go to the trash) or
sends it to review. The job's idempotency key is the folder plus its content hash: an album
already queued, imported or rejected in review is never queued again, a changed one is.
"""

import asyncio
import hashlib
import json
import logging
import os
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.migration import Group, scan
from mixsync.acquire.pipeline import Handler
from mixsync.acquire.requests import VERIFY_ATTEMPTS, candidate_dict
from mixsync.core.clock import Clock
from mixsync.core.jobs import LIVE, JobKind
from mixsync.core.matching import Candidate, CandidateFile
from mixsync.db.models.auth import User
from mixsync.db.models.work import Job, Request
from mixsync.db.queue import JobRecord, enqueue_in

log = logging.getLogger(__name__)

INTERVAL = timedelta(minutes=5)
MAX_ATTEMPTS = 3  # a failing album is queued this often, then left alone
SETTLE = timedelta(seconds=60)  # a file touched more recently may still be being copied in
Rng = Callable[[float, float], float]


def _digest(g: Group, hashes: dict[str, str]) -> str:
    files = sorted((rel, hashes.get(str(Path(g.folder) / rel), "")) for rel, _ in g.files)
    return hashlib.sha256(json.dumps([g.folder, files]).encode()).hexdigest()[:32]


@dataclass(frozen=True)
class DropFolder:
    sessions: sessionmaker[Session]
    clock: Clock
    drop_dir: Path | None
    work_dir: Path  # where the scan keeps its manifest
    rng: Rng = random.uniform
    stat: Callable[[Path], os.stat_result] = Path.stat
    # the adopter (acquire/adopter.py) is this class over the downloads dir:
    kind: JobKind = JobKind.DROP_SCAN
    source: str = "drop"  # Request.source; also the idempotency key prefix
    settle: timedelta = SETTLE
    owned: Callable[[Session], set[Path]] | None = None  # dirs whose albums are not ours to take
    capped: set[str] = field(default_factory=set[str])  # digests whose retry cap was logged

    def handlers(self) -> dict[JobKind, Handler]:
        return {self.kind: self.run}

    def start_chains(self) -> None:
        """On startup: queue a scan unless the feature is off or a chain is already live."""
        if self.drop_dir is None:
            return
        with self.sessions.begin() as s:
            live = s.scalar(
                select(Job.id).where(Job.kind == self.kind, Job.status.in_(LIVE)).limit(1)
            )
            if live is None:
                enqueue_in(s, self.clock.now(), self.kind, {})

    def _quiet(self, g: Group) -> bool:
        """No file, and no folder holding one, changed within the settle time. ctime counts too:
        copies that keep the old mtime (cp -p, rsync -a, Finder, SMB) still bump it. A file that
        vanishes mid-scan makes the album not quiet yet."""
        root = Path(g.folder)
        paths = {root / rel for rel, _ in g.files}
        paths |= {q for p in paths for q in (p.parent, root) if q != p}
        try:
            newest = max(max(st.st_mtime, st.st_ctime) for st in map(self.stat, paths))
        except OSError:
            return False
        return newest <= (self.clock.now() - self.settle).timestamp()

    def _queue(self, groups: list[Group], hashes: dict[str, str], mine: set[Path]) -> int:
        now, queued = self.clock.now(), 0
        with self.sessions.begin() as s:
            ours = [
                g
                for g in groups
                if mine.isdisjoint(
                    {Path(g.folder), *(Path(g.folder, r).parent for r, _ in g.files)}
                )
            ]
            for g in groups:
                if g not in ours:
                    log.debug("%s: %s is not ours to take", self.source, g.folder)
            groups = ours
            # the first account is the admin who set mixsync up; it owns what lands in review
            owner = s.scalar(select(User.id).order_by(User.id).limit(1))
            if owner is None:
                return 0
            for g in groups:
                base = f"{self.source}:{_digest(g, hashes)}"
                earlier = list(
                    s.scalars(
                        select(Job)
                        .where(
                            Job.kind == JobKind.VERIFY,
                            Job.idempotency_key.like(f"{base}%"),
                        )
                        .order_by(Job.id)
                    )
                )
                key = base if not earlier else f"{base}:{len(earlier)}"
                if earlier:
                    last = s.get(Request, earlier[-1].payload["request_id"])
                    if len(earlier) >= MAX_ATTEMPTS and last and last.status == "failed":
                        if base not in self.capped:
                            self.capped.add(base)
                            log.info(
                                "%s: %s failed %d times, giving up",
                                self.source,
                                g.folder,
                                len(earlier),
                            )
                        continue
                    if last is None or last.status != "failed":
                        log.debug("%s: %s already queued as %s", self.source, g.folder, base)
                        continue  # a failed attempt is tried again; anything else is settled
                req = Request(
                    user_id=owner,
                    release_mbid="",
                    artist=g.artist[:255],
                    album=(g.album or Path(g.folder).name)[:255],
                    status="verifying",
                    source=self.source,
                )
                s.add(req)
                s.flush()
                files = tuple(
                    CandidateFile(rel, size, Path(rel).suffix.lower().lstrip("."))
                    for rel, size in g.files
                )
                enqueue_in(
                    s,
                    now,
                    JobKind.VERIFY,
                    {
                        "request_id": req.id,
                        "release_mbid": None,
                        "candidate": candidate_dict(Candidate("", g.folder, files)),
                        "paths": [str(Path(g.folder) / rel) for rel, _ in g.files],
                    },
                    max_attempts=VERIFY_ATTEMPTS,
                    idempotency_key=key,
                )
                queued += 1
        return queued

    def _scan_and_queue(self, root: Path) -> int:
        with self.sessions() as s:
            mine: set[Path] = self.owned(s) if self.owned else set()
        found = scan(root, self.work_dir, mine.__contains__)
        lines = (self.work_dir / "manifest.jsonl").read_text().splitlines()
        hashes = {str(root / e["path"]): e["sha256"] for e in map(json.loads, lines)}
        if found["unreadable"]:  # left where they are, never deleted
            bad = [u["path"] for u in found["unreadable"]]
            log.warning(
                "%s: %d unreadable file(s) left in place: %s", self.source, len(bad), bad[:5]
            )
        return self._queue([g for g in found["groups"] if self._quiet(g)], hashes, mine)

    async def run(self, job: JobRecord) -> None:
        try:
            if self.drop_dir is not None and self.drop_dir.is_dir():
                # shortcut: hashes the whole folder every scan; add an mtime/size cache if a
                # drop folder ever holds terabytes nobody has dealt with
                if n := await asyncio.to_thread(self._scan_and_queue, self.drop_dir):
                    log.info("%s: queued %d album(s)", self.source, n)
        finally:  # the chain goes on after a failure too; the key makes a retry add no second
            now = self.clock.now()
            with self.sessions.begin() as s:
                enqueue_in(
                    s,
                    now,
                    self.kind,
                    {},
                    run_after=now + INTERVAL * self.rng(0.75, 1.25),
                    idempotency_key=f"{self.source}-scan:after:{job.id}",
                )
