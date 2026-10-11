"""Merging a fragmented release group onto its canonical release, and undoing it.

`build` makes a draft plan (the dry run; no side effects). `execute` and `undo_plan` run as the
MERGE and UNDO_MERGE jobs. Both are resumable: every op is validated against the DB and the
file journal before it acts, a file leaves its path only through FileOps (so the original of a
move or trash lands in `.trash`), and an op's Track-row change commits together with its `done`
flag. A validation failure marks the plan failed and stops: nothing is guessed.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.requests import enqueue_rescan
from mixsync.core.clock import Clock
from mixsync.core.errors import SafetyError
from mixsync.core.jobs import JobKind, JobState
from mixsync.core.protocols import MetadataProvider
from mixsync.db import canonical as canonical_db
from mixsync.db.models.library import CanonicalRelease, Plan, Track
from mixsync.db.models.safety import FileOp
from mixsync.db.models.work import Job
from mixsync.db.queue import JobRecord, enqueue_in
from mixsync.library import canonical, fragmentation
from mixsync.library.fileops import FileOps
from mixsync.library.importer import snapshotting_writer

LIVE = (JobState.QUEUED, JobState.LEASED, JobState.RUNNING, JobState.RETRY_WAIT)


def live_job(s: Session, plan_id: int) -> bool:
    """Is a MERGE or UNDO_MERGE job for this plan queued or running?"""
    jobs = s.scalars(
        select(Job).where(Job.kind.in_([JobKind.MERGE, JobKind.UNDO_MERGE]), Job.status.in_(LIVE))
    )
    return any(j.payload.get("plan_id") == plan_id for j in jobs)


def _open_plan(s: Session, group: str) -> str | None:
    """Why this group cannot get a new plan: one is running or being undone, or a draft has been
    confirmed (its job is waiting). An unconfirmed draft is an abandoned preview."""
    plans = s.scalars(
        select(Plan).where(
            Plan.release_group_mbid == group, Plan.status.in_(["draft", "running", "undoing"])
        )
    )
    for p in plans:
        if p.status != "draft" or live_job(s, p.id):
            return f"plan {p.id} for release group {group} is {p.status}"
    return None


def _enqueue(s: Session, now: datetime, kind: JobKind, plan_id: int) -> None:
    # One key per plan state for merge and undo alike, so two requests racing on the same plan
    # enqueue one job. The plan's last change is in it: a re-queue after a failure is new.
    stamp = s.get_one(Plan, plan_id).updated_at.isoformat()
    enqueue_in(s, now, kind, {"plan_id": plan_id}, idempotency_key=f"plan:{plan_id}:{stamp}")


def enqueue_merge(s: Session, now: datetime, plan_id: int) -> None:
    _enqueue(s, now, JobKind.MERGE, plan_id)


def enqueue_undo(s: Session, now: datetime, plan_id: int) -> None:
    _enqueue(s, now, JobKind.UNDO_MERGE, plan_id)


class PlanInProgress(ValueError):
    """The group has a merge plan that is running or being undone."""


@dataclass(frozen=True)
class Merger:
    sessions: sessionmaker[Session]
    clock: Clock
    metadata: MetadataProvider
    fileops: FileOps
    template: str
    canonical_media: tuple[str, ...] = ("Digital Media", "CD")
    canonical_countries: tuple[str, ...] = ("XW",)

    def handlers(self) -> dict[JobKind, Callable[[JobRecord], Any]]:
        return {JobKind.MERGE: self.merge, JobKind.UNDO_MERGE: self.undo}

    def fragments(self) -> list[fragmentation.Fragment]:
        with self.sessions() as s:
            return fragmentation.find(s)

    async def build(self, group: str) -> int:
        """Store a draft merge plan for the group and return its id. Reads the DB, fetches at
        most one browse (only without a stored canonical) and one release, stores no canonical."""
        with self.sessions() as s:
            stored = canonical_db.get(s, group)
            tracks = list(s.scalars(select(Track).where(Track.release_group_mbid == group)))
            rows = set(s.scalars(select(Track.path)))
            busy = _open_plan(s, group)
        if busy:
            raise PlanInProgress(busy)
        if not any(t.status == "verified" for t in tracks):
            raise LookupError(f"no verified tracks in release group {group}")
        cid = stored
        if cid is None:
            releases = await self.metadata.browse_group_releases(group)
            if not releases:
                raise LookupError(f"MusicBrainz lists no releases for {group}")
            cid = canonical.choose(releases, self.canonical_media, self.canonical_countries)
        album = await self.metadata.get_release(cid)
        ops = fragmentation.build_ops(
            tracks, album, self.template, lambda r: r in rows or self.fileops.has(r), self._size
        )
        with self.sessions.begin() as s:
            if busy := _open_plan(s, group):  # confirmed since we looked
                raise PlanInProgress(busy)
            s.execute(  # abandoned previews: never executed, so DB only
                update(Plan)
                .where(
                    Plan.kind == "merge", Plan.release_group_mbid == group, Plan.status == "draft"
                )
                .values(status="discarded")
            )
            plan = Plan(
                kind="merge",
                release_group_mbid=group,
                canonical_release_mbid=cid,
                previous_canonical_mbid=stored,
                ops=ops,
            )
            s.add(plan)
            s.flush()
            plan.batch_id = f"merge-{plan.id}"
            return plan.id

    def _size(self, rel: str) -> int:
        try:
            return (self.fileops.library / rel).stat().st_size
        except OSError:
            return 0

    async def merge(self, job: JobRecord) -> None:
        await asyncio.to_thread(self.execute, job.payload["plan_id"])

    async def undo(self, job: JobRecord) -> None:
        await asyncio.to_thread(self.undo_plan, job.payload["plan_id"])

    # -- execute -----------------------------------------------------------------------------

    def execute(self, plan_id: int) -> None:
        with self.sessions.begin() as s:
            plan = s.get_one(Plan, plan_id)
            if plan.status == "done":
                return
            undo_failed = plan.status == "failed" and (plan.error or "").startswith("undo:")
            # shortcut: "running" is accepted so a crashed run resumes, but with several worker
            # processes a lost lease can let a merge and its undo overlap (noreplace + trash only,
            # so nothing is lost); add a plan-level lock before running more than one worker.
            if plan.status not in ("draft", "running", "failed") or undo_failed:
                raise SafetyError(f"plan {plan_id} is {plan.status}")
            plan.status, plan.error = "running", None
            promote = plan.kind == "promote"  # files move onto a release; canonical is left alone
            before = canonical_db.get(s, plan.release_group_mbid)
            ok = promote or self._set_canonical(s, plan)
            if ok and not promote and before != plan.canonical_release_mbid:
                plan.canonical_set = True
            batch, ops = plan.batch_id, plan.ops
        try:
            if not ok:
                raise SafetyError("the canonical release changed since the plan was built")
            for i, op in enumerate(ops):
                if op["op"] in ("move", "trash") and not op["done"]:
                    if not promote:
                        self._canonical_unchanged(plan_id)
                    (self._move if op["op"] == "move" else self._trash)(plan_id, batch, i, op)
        except Exception as e:  # a crash (BaseException) leaves "running": a retry resumes
            self._fail(plan_id, str(e) or repr(e))
            raise
        with self.sessions.begin() as s:
            s.get_one(Plan, plan_id).status = "done"
            enqueue_rescan(s, self.clock.now())

    def _fail(self, plan_id: int, error: str) -> None:
        with self.sessions.begin() as s:
            plan = s.get_one(Plan, plan_id)
            plan.status, plan.error = "failed", error

    def _set_canonical(self, s: Session, plan: Plan) -> bool:
        group, new, prev = (
            plan.release_group_mbid,
            plan.canonical_release_mbid,
            plan.previous_canonical_mbid,
        )
        cur = canonical_db.get(s, group)
        if cur == new:  # also a resumed run
            return True
        if cur != prev:
            return False
        if prev is None:
            return canonical_db.store(s, group, new) == new
        return canonical_db.switch(s, group, prev, new) == new

    def _canonical_unchanged(self, plan_id: int) -> None:
        with self.sessions() as s:
            plan = s.get_one(Plan, plan_id)
            if canonical_db.get(s, plan.release_group_mbid) != plan.canonical_release_mbid:
                raise SafetyError("the canonical release changed while the plan ran")

    def _row(self, op: dict[str, Any]) -> None:
        """The op's Track row must still be exactly as the plan saw it."""
        with self.sessions() as s:
            row = s.scalar(select(Track).where(Track.path == op["old"]["path"]))
            if row is None or fragmentation.row_dict(row) != op["old"]:
                raise SafetyError(f"track row for {op['old']['path']} changed since the plan")

    def _move(self, plan_id: int, batch: str, i: int, op: dict[str, Any]) -> None:
        """Trash the original, then copy it from the trash to its new place. The journaled trash
        is the retry key, so a resumed run never looks at what is on disk at the old path."""
        fo, old, new = self.fileops, op["old"]["path"], op["new_path"]
        self._row(op)
        trashed = self._journaled(batch, "trash", src=old)
        if trashed is None:
            with self.sessions() as s:
                taken = s.scalar(
                    select(Track.id).where(Track.path == new, Track.id != op["track_id"])
                )
            if not fo.has(old):
                raise SafetyError(f"{old} is gone")
            if taken is not None or (new != old and fo.has(new)):  # checked before anything moves
                raise SafetyError(f"{new} already exists")
            fo.trash(old, batch)
            trashed = self._journaled(batch, "trash", src=old)
        if trashed is None:
            raise SafetyError(f"{old} was not trashed")
        src = fo.trash_dir / trashed.dst
        if fo.find_imported(batch, src) != new:
            if fo.has(new):
                raise SafetyError(f"{new} already exists")
            tagset = fragmentation.tagset_from_dict(op["tags"])
            fo.import_file(
                src, new, snapshotting_writer(self.sessions, self.clock, new, tagset), batch
            )

        def apply(s: Session, row: Track) -> None:
            tg = op["tags"]
            row.path, row.release_mbid = new, tg["release_id"]
            row.artist_mbids = list(tg["artist_ids"])
            # a promotion changes these (a merge keeps them)
            row.recording_mbid, row.release_group_mbid = tg["recording_id"], tg["release_group_id"]
            row.acoustid_id, row.status = tg["acoustid_id"], tg["status"]

        self._commit_op(plan_id, i, op, apply, done=True)

    def _trash(self, plan_id: int, batch: str, i: int, op: dict[str, Any]) -> None:
        fo, old = self.fileops, op["old"]["path"]
        self._row(op)
        with self.sessions() as s:
            keeper = s.scalar(select(Track.id).where(Track.path == op["keeper"]))
        if keeper is None or not fo.has(op["keeper"]):  # never trash the last copy
            raise SafetyError(f"the copy to keep ({op['keeper']}) is missing")
        if self._journaled(batch, "trash", src=old) is None:
            if not fo.has(old):
                raise SafetyError(f"{old} is gone")
            fo.trash(old, batch)
        self._commit_op(plan_id, i, op, lambda s, row: s.delete(row), done=True)

    def _commit_op(
        self,
        plan_id: int,
        i: int,
        op: dict[str, Any],
        apply: Callable[[Session, Track], None],
        *,
        done: bool,
    ) -> None:
        with self.sessions.begin() as s:
            plan = s.get_one(Plan, plan_id)
            apply(s, s.get_one(Track, op["track_id"]))
            plan.ops = [*plan.ops[:i], {**op, "done": done}, *plan.ops[i + 1 :]]

    def _journaled(
        self, batch: str, kind: str, *, src: str | None = None, dst: str | None = None
    ) -> FileOp | None:
        """A completed file op of this batch."""
        q = select(FileOp).where(
            FileOp.batch_id == batch, FileOp.kind == kind, FileOp.status == "done"
        )
        if src is not None:
            q = q.where(FileOp.src == src)
        if dst is not None:
            q = q.where(FileOp.dst == dst)
        with self.sessions() as s:
            return s.scalars(q.order_by(FileOp.id.desc())).first()

    # -- undo --------------------------------------------------------------------------------

    def undo_plan(self, plan_id: int) -> None:
        """Reverse everything the plan's batch did, last op first, as the journal batch
        `undo-<id>`. The journal decides, not the ops' `done` flags, so a half-done op is
        reversed too. Any problem fails the plan and leaves the library as it is."""
        with self.sessions.begin() as s:
            plan = s.get_one(Plan, plan_id)
            if plan.status == "undone":
                return
            # shortcut: "running" is accepted to clean up after a crash; see execute.
            if plan.status not in ("done", "failed", "running", "undoing"):
                raise SafetyError(f"plan {plan_id} is {plan.status}")
            plan.status = "undoing"
            batch, ops = plan.batch_id, plan.ops
        try:
            for i in reversed(range(len(ops))):
                if ops[i]["op"] in ("move", "trash"):
                    self._undo_op(plan_id, batch, f"undo-{plan_id}", i, ops[i])
        except Exception as e:  # a crash (BaseException) leaves "undoing": a retry resumes
            self._fail(plan_id, f"undo: {e or repr(e)}")
            raise
        with self.sessions.begin() as s:
            plan = s.get_one(Plan, plan_id)
            group, new, prev = (
                plan.release_group_mbid,
                plan.canonical_release_mbid,
                plan.previous_canonical_mbid,
            )
            if plan.canonical_set and canonical_db.get(s, group) == new:  # else not ours to undo
                if prev is None:
                    s.execute(
                        delete(CanonicalRelease).where(
                            CanonicalRelease.release_group_mbid == group,
                            CanonicalRelease.release_mbid == new,
                        )
                    )
                else:
                    canonical_db.switch(s, group, new, prev)
            plan.status, plan.error, plan.canonical_set = "undone", None, False
            enqueue_rescan(s, self.clock.now())

    def _undo_op(self, plan_id: int, batch: str, ubatch: str, i: int, op: dict[str, Any]) -> None:
        fo, old = self.fileops, op["old"]
        path, new = old["path"], op.get("new_path")
        trashed = self._journaled(batch, "trash", src=path)
        if trashed is None:  # the original never left its place
            return
        with self.sessions() as s:
            row = s.get(Track, op["track_id"])
        if op["op"] == "move" and (row is None or row.path not in (new, path)):
            raise SafetyError(f"track row for {new} changed since the merge")
        restored = self._journaled(ubatch, "restore", dst=path) is not None
        # The file at new_path is ours if it is the batch's copy, or if the library still lists
        # it (the user may have retagged it). Decided before anything moves.
        trash_new = new is not None and not (new == path and restored) and fo.has(new)
        if trash_new and not (
            (row is not None and row.path == new)
            or fo.find_imported(batch, fo.trash_dir / trashed.dst) == new
        ):
            raise SafetyError(f"{new} is not the merged copy; left alone")
        if trash_new and new == path:  # the merged copy is in the way
            fo.trash(path, ubatch)
        if not restored:  # refuses, touching nothing, if the old path is taken
            fo.restore(trashed.dst, path, ubatch)
        if trash_new and new != path:
            self._trash_new(ubatch, str(new))

        def apply(s: Session, row: Track) -> None:
            row.path, row.release_mbid = path, old["release_mbid"]
            row.artist_mbids = list(old["artist_mbids"])
            row.recording_mbid, row.release_group_mbid = (
                old["recording_mbid"],
                old["release_group_mbid"],
            )
            row.acoustid_id, row.status = old["acoustid_id"], old["status"]

        if row is None:  # a trash op deleted the row
            with self.sessions.begin() as s:
                s.add(Track(**{**old, "imported_at": datetime.fromisoformat(old["imported_at"])}))
        self._commit_op(plan_id, i, op, apply, done=False)

    def _trash_new(self, ubatch: str, new: str) -> None:
        self.fileops.trash(new, ubatch)
