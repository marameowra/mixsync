"""Re-checking an unverified album against MusicBrainz, and the draft plan that promotes it.

One RECHECK job per album: one search, at most three releases, one AcoustID lookup per file.
A confident match only stores a draft `promote` plan (same op format as a merge `move`); the
user confirms it and the Merger runs it, so nothing changes on disk without them. Without a
confident match a background check schedules the next one, backing off (see requests.py).
"""

import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.merge import live_job
from mixsync.acquire.pipeline import Handler, acoustid_id, file_track, likelies
from mixsync.acquire.requests import (
    AlbumKey,
    Item,
    enqueue_recheck,
    group,
    profile_for,
    read_unverified,
)
from mixsync.core.clock import Clock
from mixsync.core.errors import TransientError
from mixsync.core.jobs import JobKind, JobState
from mixsync.core.matching import AlbumInfo, Band, FileTrack, Profile
from mixsync.core.protocols import AcoustIdLookup, Fingerprinter, MetadataProvider
from mixsync.db.models.library import Plan, Track
from mixsync.db.models.work import Job
from mixsync.db.queue import JobRecord
from mixsync.library import paths
from mixsync.library.fileops import FileOps
from mixsync.library.fragmentation import row_dict
from mixsync.library.importer import render_path, tagset_for
from mixsync.match.profiles import BALANCED
from mixsync.match.scorer import assign_tracks, score_files

log = logging.getLogger(__name__)

SEARCH_LIMIT, MAX_RELEASES = 5, 3


@dataclass(frozen=True)
class Status:
    last: datetime | None  # the last finished check
    next: datetime | None  # the next background check
    plan_id: int | None  # a draft promote plan waiting for the user, or a failed one
    plan_failed: bool = False


def promote_plans(s: Session, *states: str) -> list[Plan]:
    return list(s.scalars(select(Plan).where(Plan.kind == "promote", Plan.status.in_(states))))


def _has(plan: Plan, ids: set[int]) -> bool:
    return bool(ids & {o["track_id"] for o in plan.ops})


def statuses(s: Session, albums: dict[AlbumKey, list[Item]]) -> dict[AlbumKey, Status]:
    jobs = list(s.scalars(select(Job).where(Job.kind == JobKind.RECHECK)))
    drafts = promote_plans(s, "draft", "failed")  # a failed one still offers Resume / Undo
    out: dict[AlbumKey, Status] = {}
    for key, items in albums.items():
        mine = [j for j in jobs if tuple(j.payload["album"]) == key]
        done = [j.updated_at for j in mine if j.status is JobState.SUCCEEDED]
        due = [
            j.run_after for j in mine if j.status is JobState.QUEUED and not j.payload["user_id"]
        ]
        ids = {t.id for t, _ in items}
        plan = max((p for p in drafts if _has(p, ids)), key=lambda p: p.id, default=None)
        out[key] = Status(
            max(done, default=None),
            min(due, default=None),
            plan and plan.id,
            bool(plan and plan.status == "failed"),
        )
    return out


@dataclass(frozen=True)
class Rechecker:
    sessions: sessionmaker[Session]
    clock: Clock
    metadata: MetadataProvider
    fingerprint: Fingerprinter
    acoustid: AcoustIdLookup
    fileops: FileOps
    template: str

    def handlers(self) -> dict[JobKind, Handler]:
        return {JobKind.RECHECK: self.recheck}

    async def recheck(self, job: JobRecord) -> None:
        key: AlbumKey = (*job.payload["album"],)  # pyright: ignore[reportAssignmentType]
        uid, attempt = job.payload["user_id"], job.payload["attempt"]

        def next_check(step: int = 1) -> None:
            # A user's check leaves the background schedule alone. The key makes a retry of
            # this job (or this call after a failure) add nothing.
            if uid is None:
                with self.sessions.begin() as s:
                    chain = f"recheck:next:{job.id}"
                    now = self.clock.now()
                    enqueue_recheck(s, now, key, None, attempt=attempt + step, chain_key=chain)

        try:
            with self.sessions() as s:
                items = group(read_unverified(s, self.fileops.library)).get(key, [])
                profile = profile_for(s, uid) if uid else BALANCED
            items = [i for i in items if self.fileops.has(i[0].path)]
            if not items or not key[1]:  # nothing unverified left: the chain ends
                return
            found = await self._match(key, items, profile)
            done = False
            if found:
                with self.sessions.begin() as s:
                    done = self._draft(s, items, *found)
        except BaseException as e:  # a failing run must not end the background chain
            if not isinstance(e, TransientError):
                next_check()
            elif job.attempts >= job.max_attempts:  # an outage is no "no match": same interval
                next_check(0)
            raise
        if not done:  # no match, or some files stay unverified: look again later
            next_check()

    async def _match(
        self, key: AlbumKey, items: list[Item], profile: Profile
    ) -> tuple[AlbumInfo, dict[int, FileTrack]] | None:
        """The best of the top releases, if it is confident enough to promote to. A file that
        cannot be fingerprinted or looked up is skipped (an outage retries the job); none left means no match."""
        refs = await self.metadata.search_releases(key[0], key[1], limit=SEARCH_LIMIT)
        albums = [await self.metadata.get_release(r.release_id) for r in refs[:MAX_RELEASES]]
        if not albums:
            return None
        files: dict[int, FileTrack] = {}  # by track id
        for t, _ in items:
            path = self.fileops.library / t.path
            try:
                fp = await self.fingerprint(path)
                files[t.id] = file_track(path, fp.duration, tuple(await self.acoustid.lookup(fp)))
            except Exception as e:  # a bad file is skipped; an outage must retry the job
                if isinstance(e, TransientError):
                    raise
                log.warning("re-check of %s skipped", t.path, exc_info=True)
        if not files:
            return None
        like = likelies([self.fileops.library / t.path for t, _ in items])
        scored = [(score_files(list(files.values()), like, a, profile), a) for a in albums]
        result, best = min(scored, key=lambda x: x[0].distance)
        return (best, files) if result.band is Band.AUTO_ACCEPT else None

    def _draft(
        self, s: Session, items: list[Item], album: AlbumInfo, files: dict[int, FileTrack]
    ) -> bool:
        """Store the draft plan. True if it promotes every file; False if some stay unverified
        (or no plan was made), so the caller keeps checking."""
        rows = [t for t, _ in items]
        ids = {t.id for t in rows}
        mine = [
            p
            for p in promote_plans(s, "draft", "running", "undoing", "failed")
            if _has(p, ids) and not (p.error or "").startswith("undo:")  # a stuck undo: new draft
        ]
        if any(p.status != "draft" or live_job(s, p.id) for p in mine):
            return False  # a plan for this album is confirmed or running: leave it be
        pairs, _, _ = assign_tracks(list(files.values()), album.tracks)
        paired = {id(f): t for f, t in pairs}
        in_db, taken = set(s.scalars(select(Track.path))), set[str]()
        owned = set(
            s.scalars(
                select(Track.recording_mbid).where(
                    Track.status == "verified", Track.recording_mbid.is_not(None)
                )
            )
        )
        ops: list[dict[str, Any]] = []
        for row in rows:
            f = files.get(row.id)
            op: dict[str, Any] = {"op": "keep", "reason": "not on the release; stays unverified"}
            if f is not None and id(f) in paired:
                track = paired[id(f)]
                if track.recording_id in owned:
                    op["reason"] = "already owned as verified"
                else:
                    ts = tagset_for(track, album, acoustid_id(f), "verified")
                    new = paths.unique(
                        render_path(self.template, ts, Path(row.path).suffix),
                        # the track's own path frees up: its original is trashed first
                        lambda r, own=row.path: (
                            r in taken or (r != own and (r in in_db or self.fileops.has(r)))
                        ),
                    )
                    taken.add(new)
                    op = {"op": "move", "new_path": new, "tags": asdict(ts)}
            ops.append({**op, "track_id": row.id, "old": row_dict(row), "done": False})
        if not taken:  # nothing to promote
            return False
        for p in mine:  # abandoned previews: never executed, so DB only
            p.status = "discarded"
        plan = Plan(
            kind="promote",
            release_group_mbid=album.release_group_id or "",
            canonical_release_mbid=album.release_id or "",
            ops=ops,
        )
        s.add(plan)
        s.flush()
        plan.batch_id = f"promote-{plan.id}"
        return all(o["op"] == "move" for o in ops)

    def start_chains(self) -> None:
        """On startup: every unverified album without a waiting background check gets one."""
        with self.sessions.begin() as s:
            for key in group(read_unverified(s, self.fileops.library)):
                if key[1]:
                    enqueue_recheck(s, self.clock.now(), key, None)
