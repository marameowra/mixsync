"""Merging a fragmented release group onto its canonical release, and undoing it.

`build` makes a draft plan (the dry run; no side effects) as the MERGE_PLAN job. `execute` and
`undo_plan` run as the MERGE and UNDO_MERGE jobs. Both are resumable: every op is validated
against the DB and the file journal before it acts, a file leaves its path only through FileOps
(so the original of a move or trash lands in `.trash`), and an op's Track-row change commits
together with its `done` flag. A validation failure marks the plan failed and stops: nothing is
guessed.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire import lifecycle
from mixsync.acquire.acoustid_submit import enqueue_submit
from mixsync.acquire.request_delete import NotDeletable, delete_request
from mixsync.acquire.requests import enqueue_recheck, enqueue_rescan, read_unverified
from mixsync.acquire.requests import group as by_album
from mixsync.core.clock import Clock, aware
from mixsync.core.errors import SafetyError, TransientError
from mixsync.core.jobs import LIVE, JobKind
from mixsync.core.protocols import MetadataProvider
from mixsync.db import canonical as canonical_db
from mixsync.db import progress
from mixsync.db.models.library import CanonicalRelease, Plan, Track
from mixsync.db.models.safety import FileOp
from mixsync.db.models.work import Job
from mixsync.db.models.work import Request as AlbumRequest
from mixsync.db.names import stored_names
from mixsync.db.queue import JobRecord, enqueue_in
from mixsync.db.undo import undoable
from mixsync.library import canonical, fragmentation, layout, paths, tags
from mixsync.library.fileops import FileOps
from mixsync.library.importer import snapshotting_writer


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
            name = stored_names(s, {group}).get(group, group)  # no lookup: the UUID if unnamed
            return f"plan {p.id} for release group {name} is {p.status}"
    return _delete_blocking(s, {group}, set(), "delete")  # a delete plan blocks merges too


DELETE_DRAFT_DAYS = 7  # an unconfirmed delete preview older than this is stale


def stale_delete_draft(s: Session, plan: Plan, now: datetime) -> bool:
    """A delete preview nobody confirmed (no job waiting) for over DELETE_DRAFT_DAYS days."""
    return (
        plan.kind == "delete"
        and plan.status == "draft"
        and now - aware(plan.created_at) > timedelta(days=DELETE_DRAFT_DAYS)
        and not live_job(s, plan.id)
    )


def _delete_blocking(s: Session, groups: set[str], ids: set[int], kind: str | None) -> str | None:
    """Why these release groups or tracks cannot get a plan: a plan (of this kind, if given)
    that is confirmed, running, undoing or failed touches them. Unlike `_open_plan`, a failed
    plan blocks too."""
    q = select(Plan).where(Plan.status.in_(["draft", "running", "undoing", "failed"]))
    for p in s.scalars(q if kind is None else q.where(Plan.kind == kind)):
        if p.status == "draft" and not live_job(s, p.id):
            continue
        theirs = {p.release_group_mbid} | {
            o["old"]["release_group_mbid"] for o in p.ops if "old" in o
        }
        if groups & theirs or ids & {o["track_id"] for o in p.ops if "track_id" in o}:
            return f"plan {p.id} is {p.status} for the same music"
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


def enqueue_reorganize(s: Session, now: datetime) -> None:
    """Queue the job that builds a reorganize draft, unless one is queued or running. The
    payload holds the newest plan id so far: the draft is the first reorganize plan after it."""
    live = select(Job.id).where(Job.kind == JobKind.REORGANIZE_PLAN, Job.status.in_(LIVE))
    if s.scalar(live.limit(1)) is None:
        after = s.scalar(select(func.max(Plan.id))) or 0
        enqueue_in(s, now, JobKind.REORGANIZE_PLAN, {"after": after}, max_attempts=1)


def enqueue_merge_plan(s: Session, now: datetime, group: str) -> None:
    """Queue the job that builds a merge draft for the group, unless one is queued or running.
    Raises PlanInProgress at once (a DB read) if the group cannot get a new plan."""
    if busy := _open_plan(s, group):
        raise PlanInProgress(busy)
    job = merge_build(s, group)[0]
    if job is None or job.status not in LIVE:  # a second click points at the queued build
        after = s.scalar(select(func.max(Plan.id))) or 0
        enqueue_in(s, now, JobKind.MERGE_PLAN, {"group": group, "after": after})


def merge_build(s: Session, group: str) -> tuple[Job | None, int | None]:
    """The group's newest merge build job and the draft it made, if it has made one."""
    job = s.scalars(
        select(Job)
        .where(Job.kind == JobKind.MERGE_PLAN, Job.payload["group"].as_string() == group)
        .order_by(Job.id.desc())
        .limit(1)
    ).first()
    if job is None:
        return None, None
    # max: a double click can queue two builds; the later one discards the earlier draft
    made = select(func.max(Plan.id)).where(
        Plan.kind == "merge", Plan.release_group_mbid == group, Plan.id > job.payload["after"]
    )
    return job, s.scalar(made)


def reorganize_build(s: Session) -> tuple[Job | None, int | None]:
    """The newest reorganize build job and the draft it made, if it has made one."""
    job = s.scalars(
        select(Job).where(Job.kind == JobKind.REORGANIZE_PLAN).order_by(Job.id.desc())
    ).first()
    if job is None:
        return None, None
    made = select(func.min(Plan.id)).where(
        Plan.kind == "reorganize", Plan.id > job.payload.get("after", 0)
    )
    return job, s.scalar(made)


def _device(path: Path) -> int:
    """The filesystem holding the path, or its nearest existing parent."""
    while not path.exists() and path != path.parent:
        path = path.parent
    return path.stat().st_dev


def _no_tags(_staging: Path, _op_id: int) -> None:
    """A reorganize move keeps the file's tags as they are."""


class PlanInProgress(ValueError):
    """The group has a merge plan that is running or being undone."""


class DeleteRefused(ValueError):
    """A delete plan cannot be made: a request is still working on the group, or a file is
    missing or outside the library."""


@dataclass(frozen=True)
class Merger:
    sessions: sessionmaker[Session]
    clock: Clock
    metadata: MetadataProvider
    fileops: FileOps
    template: str
    canonical_media: tuple[str, ...] = ("Digital Media", "CD")
    canonical_countries: tuple[str, ...] = ("XW",)
    trash_days: int = 30  # an undo is possible this long after the plan finished
    device: Callable[[Path], int] = _device  # tests fake a second mount

    def handlers(self) -> dict[JobKind, Callable[[JobRecord], Any]]:
        return {
            JobKind.MERGE: self.merge,
            JobKind.UNDO_MERGE: self.undo,
            JobKind.REORGANIZE_PLAN: self.plan_reorganize,
            JobKind.MERGE_PLAN: self.plan_merge,
        }

    async def plan_merge(self, job: JobRecord) -> None:
        try:
            await self.build(job.payload["group"])
        except httpx.HTTPError as e:  # MusicBrainz slow or down: the queue retries with backoff
            raise TransientError(f"MusicBrainz is unavailable ({type(e).__name__})") from e

    async def plan_reorganize(self, job: JobRecord) -> None:
        await asyncio.to_thread(self.build_reorganize)

    def fragments(self) -> list[fragmentation.Fragment]:
        with self.sessions() as s:
            return fragmentation.find(s)

    async def build(self, group: str) -> int:
        """Store a draft merge plan for the group and return its id. Reads the DB, fetches at
        most one browse (only without a stored canonical) and one release, stores no canonical."""
        with self.sessions() as s:
            stored = canonical_db.get(s, group)
            tracks = list(s.scalars(select(Track).where(Track.release_group_mbid == group)))
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

        def taken(r: str) -> bool:  # one indexed lookup per candidate path, not every track path
            with self.sessions() as s:
                return s.scalar(select(Track.id).where(Track.path == r).limit(1)) is not None

        ops = fragmentation.build_ops(
            tracks, album, self.template, lambda r: taken(r) or self.fileops.has(r), self._size
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

    def build_delete(self, track_ids: list[int], request: AlbumRequest | None = None) -> int:
        """Store a draft delete plan for these library tracks and return its id (the dry run: no
        side effects). Confirm trashes the files and drops the rows; undo restores both. With a
        request, a last `request` op records it for the worker to delete when the plan is done."""
        with self.sessions.begin() as s:
            now = self.clock.now()
            for p in s.scalars(select(Plan).where(Plan.kind == "delete", Plan.status == "draft")):
                if stale_delete_draft(s, p, now):  # never executed, so DB only
                    p.status = "discarded"
            tracks = list(
                s.scalars(select(Track).where(Track.id.in_(track_ids)).order_by(Track.id))
            )
            if not tracks or len(tracks) != len(set(track_ids)):
                raise LookupError("no such tracks")
            groups = {t.release_group_mbid for t in tracks if t.release_group_mbid}
            busy = select(AlbumRequest.id).where(
                AlbumRequest.status.in_(lifecycle.BUSY),
                AlbumRequest.release_group_mbid.in_(groups)
                | (AlbumRequest.id == (request.id if request else None)),
            )
            if s.scalar(busy.limit(1)) is not None:
                raise DeleteRefused("a request for this release group is downloading or importing")
            if blocked := _delete_blocking(s, groups, {t.id for t in tracks}, None):
                raise PlanInProgress(blocked)
            for t in tracks:
                try:  # inside the library root, no symlinks
                    present = self.fileops.has(t.path)
                except SafetyError:
                    raise DeleteRefused(f"{t.path} is outside the library") from None
                if not present:
                    raise DeleteRefused(f"{t.path} is missing from the library")
            ops: list[dict[str, Any]] = [
                {
                    "op": "trash",
                    "keeper": None,
                    "track_id": t.id,
                    "old": fragmentation.row_dict(t),
                    "done": False,
                }
                for t in tracks
            ]
            if request is not None:
                ops.append({"op": "request", "request_id": request.id, "user_id": request.user_id})
            plan = Plan(
                kind="delete",
                release_group_mbid=min(groups, default=""),
                canonical_release_mbid="",
                ops=ops,
            )
            s.add(plan)
            s.flush()
            plan.batch_id = f"delete-{plan.id}"
            return plan.id

    def backfill_templates(self) -> None:
        """On startup: rows from before ADR 0019 get the configured template. Moves nothing."""
        with self.sessions.begin() as s:
            layout.backfill(s, self.template)

    def build_reorganize(self) -> int:
        """Store a draft plan that refiles every track whose path came from another template,
        and return its id (the dry run: reads tags, writes no file). Tags are not changed.
        A move onto another filesystem is listed as `refused`, never planned."""
        fo, lib = self.fileops, self.fileops.library
        with self.sessions() as s:
            tracks = list(
                s.scalars(select(Track).where(layout.drifted(self.template)).order_by(Track.path))
            )
            rows = set(s.scalars(select(Track.path)))
            busy = _delete_blocking(s, set(), {t.id for t in tracks}, None)
        if not tracks:
            raise LookupError("every track already follows the path template")
        if busy:
            raise PlanInProgress(busy)
        taken: set[str] = set()
        ops: list[dict[str, Any]] = []
        for t in tracks:
            op: dict[str, Any] = {"op": "keep", "template": self.template}
            try:
                if not fo.has(t.path):
                    raise FileNotFoundError(t.path)
                new = paths.unique(  # the track's own path is free: its original is trashed first
                    layout.render(self.template, lib / t.path, t.path),
                    lambda r, own=t.path: r in taken or (r != own and (r in rows or fo.has(r))),
                )
            except Exception as e:  # missing, unreadable or outside the library: left alone
                op = {"op": "refused", "reason": f"cannot read the file ({e!r})"}
            else:
                if self.device(lib / t.path) != self.device(lib / new):
                    op = {"op": "refused", "new_path": new, "reason": "on another filesystem"}
                elif new != t.path:
                    taken.add(new)
                    op = {"op": "move", "new_path": new, "template": self.template}
            ops.append({**op, "track_id": t.id, "old": fragmentation.row_dict(t), "done": False})
        with self.sessions.begin() as s:
            if busy := _delete_blocking(s, set(), {t.id for t in tracks}, None):
                raise PlanInProgress(busy)
            for p in s.scalars(select(Plan).where(Plan.kind == "reorganize")):
                if p.status == "draft" and not live_job(s, p.id):  # never executed: DB only
                    p.status = "discarded"
            plan = Plan(
                kind="reorganize", release_group_mbid="", canonical_release_mbid="", ops=ops
            )
            s.add(plan)
            s.flush()
            plan.batch_id = f"reorganize-{plan.id}"
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
            merge = plan.kind == "merge"  # only a merge changes the canonical release
            # checked before the canonical is touched: a stale plan changes nothing
            stale = any(op.get("template", self.template) != self.template for op in plan.ops)
            before = canonical_db.get(s, plan.release_group_mbid)
            ok = stale or not merge or self._set_canonical(s, plan)
            if ok and not stale and merge and before != plan.canonical_release_mbid:
                plan.canonical_set = True
            batch, ops, done = plan.batch_id, plan.ops, progress.done_flags(s, plan)
        try:
            if stale:
                raise SafetyError("the path template changed since the plan was built")
            if not ok:
                raise SafetyError("the canonical release changed since the plan was built")
            self._keep_templates(plan_id, ops, done)
            for i, op in enumerate(ops):
                if op["op"] in ("move", "trash") and not done[i]:
                    if merge:
                        self._canonical_unchanged(plan_id)
                    (self._move if op["op"] == "move" else self._trash)(plan_id, batch, i, op)
        except Exception as e:  # a crash (BaseException) leaves "running": a retry resumes
            # a SafetyError precondition fails the same way again: marked, so only undo is offered
            self._fail(
                plan_id, ("blocked: " if isinstance(e, SafetyError) else "") + (str(e) or repr(e))
            )
            raise
        with self.sessions.begin() as s:
            plan = s.get_one(Plan, plan_id)
            plan.status = "done"
            plan.error = self._delete_requests(s, plan)
            enqueue_rescan(s, self.clock.now())
            if promote:  # AcoustID learns the new recording links
                enqueue_submit(s, self.clock.now(), plan_id)

    def _delete_requests(self, s: Session, plan: Plan) -> str | None:
        """The request goes last, with the plan's `done`. A refusal (it changed since confirm)
        keeps the request and is returned for the plan page; the files stay trashed."""
        for op in plan.ops:
            if op["op"] == "request" and (req := s.get(AlbumRequest, op["request_id"])):
                try:
                    with s.begin_nested():  # a refusal undoes its own partial changes
                        delete_request(s, req, op["user_id"])
                except NotDeletable as e:
                    return f"request kept: {e}"
        return None

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

    @staticmethod
    def _same(row: Track | None, want: dict[str, Any]) -> bool:
        """The row is as `want` says, in the fields it records (a plan from before a new column
        does not compare it)."""
        now = fragmentation.row_dict(row) if row is not None else None
        return now is not None and {k: now[k] for k in want} == want

    def _row(self, op: dict[str, Any]) -> None:
        """The op's Track row must still be exactly as the plan saw it."""
        with self.sessions() as s:
            row = s.scalar(select(Track).where(Track.path == op["old"]["path"]))
            if not self._same(row, op["old"]):
                raise SafetyError(f"track row for {op['old']['path']} changed since the plan")

    @staticmethod
    def _keeps(ops: list[dict[str, Any]], done: list[bool], want: bool) -> list[int]:
        """A reorganize's files already in place (done or not): only their rows change."""
        return [
            i
            for i, op in enumerate(ops)
            if op["op"] == "keep" and "template" in op and done[i] == want
        ]

    def _keep_templates(self, plan_id: int, ops: list[dict[str, Any]], done: list[bool]) -> None:
        """Record the template on every in-place track at once. A row changed since the plan
        refuses them all."""
        if not (todo := self._keeps(ops, done, False)):
            return
        with self.sessions.begin() as s:
            for i in todo:
                old = ops[i]["old"]
                row = s.scalar(select(Track).where(Track.path == old["path"]))
                if row is None or not self._same(row, old):
                    raise SafetyError(f"track row for {old['path']} changed since the plan")
                row.path_template = ops[i]["template"]
                progress.mark(s, plan_id, i, True)

    def _undo_keep_templates(
        self, plan_id: int, ops: list[dict[str, Any]], done: list[bool]
    ) -> list[str]:
        """Put the old template back on in-place tracks at once. A row changed since the plan
        ran is left as it is and returned, like a move undo refusing a changed row."""
        left: list[str] = []
        if not (todo := self._keeps(ops, done, True)):
            return left
        with self.sessions.begin() as s:
            for i in todo:
                op = ops[i]
                row = s.get(Track, op["track_id"])
                if self._same(row, {**op["old"], "path_template": op["template"]}) and row:
                    row.path_template = op["old"].get("path_template")
                else:
                    left.append(op["old"]["path"])
                progress.mark(s, plan_id, i, False)
        return left

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
            if self.device(fo.library / old) != self.device(fo.library / new):
                raise SafetyError(f"{old} -> {new} would cross filesystems")
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
            tg = op.get("tags")  # none on a reorganize move: the copy keeps the original's bytes
            write = _no_tags
            if tg is not None:
                tagset = fragmentation.tagset_from_dict(tg)
                write = snapshotting_writer(self.sessions, self.clock, new, tagset)
            fo.import_file(src, new, write, batch)

        def apply(s: Session, row: Track) -> None:
            row.path, row.path_template = new, op.get("template", self.template)
            tg = op.get("tags")
            if tg is None:  # a reorganize: only the place changed
                return
            row.release_mbid = tg["release_id"]
            row.artist_mbids = list(tg["artist_ids"])
            row.album_artist, row.album, row.year = tags.names_of(
                fragmentation.tagset_from_dict(tg)
            )
            if s.get_one(Plan, plan_id).kind == "promote":  # a merge keeps the rest of the row
                row.recording_mbid = tg["recording_id"]
                row.release_group_mbid = tg["release_group_id"]
                row.acoustid_id, row.status = tg["acoustid_id"], tg["status"]

        self._commit_op(plan_id, i, op, apply, done=True)

    def _trash(self, plan_id: int, batch: str, i: int, op: dict[str, Any]) -> None:
        fo, old = self.fileops, op["old"]["path"]
        self._row(op)
        if op["keeper"] is not None:  # None on a delete plan's ops: removing the music is the point
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
        with self.sessions.begin() as s:  # the row change commits with the op's progress
            apply(s, s.get_one(Track, op["track_id"]))
            progress.mark(s, plan_id, i, done)

    def _journaled(
        self, batch: str, kind: str, *, src: str | None = None, dst: str | None = None
    ) -> FileOp | None:
        """A completed file op of this batch."""
        q = select(FileOp).where(
            FileOp.batch_id == batch,
            FileOp.kind == kind,
            FileOp.status == "done",
            FileOp.root == "library",
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
            if not undoable(plan, self.clock.now(), self.trash_days):
                raise SafetyError(f"undo window of {self.trash_days} days has passed")
            batch, ops, kind = plan.batch_id, plan.ops, plan.kind
            done = progress.done_flags(s, plan)
        try:
            # Before anything moves: a trashed original already purged cannot be restored.
            for op in ops:
                if op["op"] not in ("move", "trash"):
                    continue
                trashed = self._journaled(batch, "trash", src=op["old"]["path"])
                if (
                    trashed is not None
                    and self._journaled(f"undo-{plan_id}", "restore", dst=op["old"]["path"]) is None
                    and not (self.fileops.trash_dir / trashed.dst).is_file()
                ):
                    raise SafetyError(
                        f"the trashed original of {op['old']['path']} is gone; nothing was moved"
                    )
            with self.sessions.begin() as s:
                s.get_one(Plan, plan_id).status = "undoing"
            for i in reversed(range(len(ops))):
                if ops[i]["op"] in ("move", "trash"):
                    self._undo_op(plan_id, batch, f"undo-{plan_id}", i, ops[i])
            left = self._undo_keep_templates(plan_id, ops, done)
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
            plan.status, plan.canonical_set = "undone", False
            plan.error = (
                f"{len(left)} tracks changed since the plan ran kept their template: "
                + ", ".join(left[:20])
                if left
                else None
            )
            enqueue_rescan(s, self.clock.now())
            if kind == "promote":  # unverified again: look for them on MusicBrainz later
                ids = {op["track_id"] for op in ops}
                for key, items in by_album(read_unverified(s)).items():
                    if key[1] and ids & {t.id for t in items}:
                        enqueue_recheck(s, self.clock.now(), key, None)

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
        if not restored and not (fo.trash_dir / trashed.dst).is_file():
            raise SafetyError(f"the trashed original of {path} is gone; nothing was moved")
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
            fo.trash(str(new), ubatch)

        names = tags.file_names(fo.library / path)  # the restored original's own tags

        def apply(s: Session, row: Track) -> None:
            row.path, row.release_mbid = path, old["release_mbid"]
            row.path_template = old.get("path_template", row.path_template)
            row.artist_mbids = list(old["artist_mbids"])
            row.album_artist, row.album, row.year = names
            if s.get_one(Plan, plan_id).kind == "promote":
                row.recording_mbid = old["recording_mbid"]
                row.release_group_mbid = old["release_group_mbid"]
                row.acoustid_id, row.status = old["acoustid_id"], old["status"]

        if row is None:  # a trash op deleted the row
            with self.sessions.begin() as s:
                at = datetime.fromisoformat(old["imported_at"])
                s.add(
                    Track(
                        **{**old, "imported_at": at},
                        album_artist=names[0],
                        album=names[1],
                        year=names[2],
                    )
                )
        self._commit_op(plan_id, i, op, apply, done=False)
