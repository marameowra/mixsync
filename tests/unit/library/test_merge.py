import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.merge import Merger, PlanInProgress, enqueue_merge, enqueue_undo, live_job
from mixsync.core.clock import SystemClock
from mixsync.core.config import Settings
from mixsync.core.errors import SafetyError
from mixsync.core.jobs import JobKind
from mixsync.core.matching import AlbumInfo, TrackInfo
from mixsync.core.types import ReleaseRef
from mixsync.db import canonical as canonical_db
from mixsync.db.journal import SqlJournal
from mixsync.db.models.library import CanonicalRelease, Plan, Track
from mixsync.db.models.safety import FileOp
from mixsync.db.models.work import Job
from mixsync.db.undo import undoable
from mixsync.library import fileops, fragmentation, tags
from mixsync.library.fileops import FileOps
from mixsync.library.importer import Importer
from mixsync.library.purge import TrashPurger

from .conftest import MakeSrc
from .test_fileops import files, sha

GROUP, OLD, CANON = "rg", "old", "canon"


def album(release: str, year: int, *recs: str) -> AlbumInfo:
    tracks = tuple(
        TrackInfo(f"Song {r}", medium=1, medium_index=i, index=i, recording_id=r)
        for i, r in enumerate(recs, 1)
    )
    return AlbumInfo("Band", "Album", tracks, year=year, release_id=release, release_group_id=GROUP)


OLD_ALBUM = album(OLD, 2000, "r1", "r2", "r3", "r9")
CANON_ALBUM = album(CANON, 1999, "r1", "r2", "r3")


class FakeMetadata:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_release(self, mbid: str) -> AlbumInfo:
        self.calls.append(f"get:{mbid}")
        return {OLD: OLD_ALBUM, CANON: CANON_ALBUM}[mbid]

    async def browse_group_releases(self, release_group_mbid: str) -> list[ReleaseRef]:
        self.calls.append(f"browse:{release_group_mbid}")
        return [
            ReleaseRef(OLD, "Album", "Band", "2000", track_count=4),
            ReleaseRef(CANON, "Album", "Band", "1999", track_count=3),
        ]


class Boom(BaseException):
    """A hard crash: nothing in the code under test catches it."""


@dataclass
class World:
    merger: Merger
    ops: FileOps
    sessions: sessionmaker[Session]
    metadata: FakeMetadata
    importer: Importer

    def imp(self, src: Path, release: str, rec: str, batch: str = "seed") -> str:
        a = OLD_ALBUM if release == OLD else CANON_ALBUM
        track = next(t for t in a.tracks if t.recording_id == rec)
        (r,) = self.importer.import_release([(src, track, "ac-" + rec)], a, "verified", batch)
        assert r.rel
        return r.rel

    def tracks(self) -> dict[str, tuple[str, str]]:
        with self.sessions() as s:
            return {
                str(t.recording_mbid): (t.path, str(t.release_mbid))
                for t in s.scalars(select(Track).order_by(Track.id))
            }

    def plan(self, plan_id: int) -> Plan:
        with self.sessions() as s:
            return s.get_one(Plan, plan_id)

    def canonical(self) -> str | None:
        with self.sessions() as s:
            return canonical_db.get(s, GROUP)

    def lib(self) -> dict[str, str]:
        return {f: sha(self.ops.library / f) for f in files(self.ops.library)}

    def trashed(self) -> list[str]:
        return files(self.ops.trash_dir)


@pytest.fixture
def world(
    ops: FileOps, sessions: sessionmaker[Session], make_src: MakeSrc, settings: Settings
) -> Iterator[World]:
    md = FakeMetadata()
    template = settings.path_template
    w = World(
        Merger(sessions, SystemClock(), md, ops, template),
        ops,
        sessions,
        md,
        Importer(ops, sessions, SystemClock(), template),
    )
    w.imp(make_src("flac", "c3"), CANON, "r3")
    for rec in ("r1", "r2", "r3", "r9"):
        w.imp(make_src("flac", "o" + rec), OLD, rec)
    md.calls.clear()
    yield w


def build(w: World) -> int:
    return asyncio.run(w.merger.build(GROUP))


def ops_by_kind(w: World, plan_id: int) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for op in w.plan(plan_id).ops:
        out.setdefault(op["op"], []).append(op)
    return out


def test_find(world: World) -> None:
    with world.sessions.begin() as s:
        for path, rec, rel, grp, status in [
            ("x", "q", "solo", "g2", "verified"),
            ("y", "q2", "u1", "g3", "unverified"),
            ("z", "q3", "u2", "g3", "verified"),
        ]:
            s.add(
                Track(path=path, recording_mbid=rec, release_mbid=rel, release_group_mbid=grp,
                      status=status, batch_id="b", imported_at=SystemClock().now())
            )  # fmt: skip
        s.add(CanonicalRelease(release_group_mbid="g2", release_mbid="elsewhere"))
    assert world.merger.fragments() == [
        fragmentation.Fragment("g2", "elsewhere", {"solo": 1}),
        fragmentation.Fragment(GROUP, None, {OLD: 4, CANON: 1}),
    ]


def test_build_plan_is_a_dry_run(world: World) -> None:
    before = world.lib()
    pid = build(world)
    assert world.metadata.calls == [f"browse:{GROUP}", f"get:{CANON}"]  # one browse, one release
    assert world.canonical() is None and world.lib() == before and world.trashed() == []
    plan = world.plan(pid)
    assert (plan.status, plan.canonical_release_mbid, plan.previous_canonical_mbid) == (
        "draft", CANON, None,
    )  # fmt: skip
    kinds = ops_by_kind(world, pid)
    assert [o["old"]["recording_mbid"] for o in kinds["trash"]] == ["r3"]
    assert kinds["trash"][0]["keeper"] == "Band/Album (1999)/01-03 Song r3.flac"
    assert [o["old"]["recording_mbid"] for o in kinds["move"]] == ["r1", "r2"]
    assert kinds["move"][0]["new_path"] == "Band/Album (1999)/01-01 Song r1.flac"
    assert kinds["move"][0]["tags"]["release_id"] == CANON
    assert [o["old"]["recording_mbid"] for o in kinds["keep"]] == ["r9"]


def test_stored_canonical_skips_the_browse(world: World) -> None:
    with world.sessions.begin() as s:
        canonical_db.store(s, GROUP, CANON)
    build(world)
    assert world.metadata.calls == [f"get:{CANON}"]


def test_targets_never_collide(world: World) -> None:
    # two canonical tracks that render to one path, and that path is already on disk
    tracks = tuple(
        TrackInfo("Same", medium=1, medium_index=1, recording_id=r) for r in ("r1", "r2")
    )
    canon = replace(CANON_ALBUM, tracks=tracks)
    (world.ops.library / "Band/Album (1999)/01-01 Same.flac").write_bytes(b"mine")
    with world.sessions() as s:
        old = list(s.scalars(select(Track).where(Track.release_mbid == OLD)))
    ops = fragmentation.build_ops(old, canon, world.merger.template, world.ops.has)
    assert [o["new_path"] for o in ops if o["op"] == "move"] == [
        "Band/Album (1999)/01-01 Same (2).flac",
        "Band/Album (1999)/01-01 Same (3).flac",
    ]


def test_execute_moves_retags_and_trashes(world: World) -> None:
    before = world.lib()
    old = world.tracks()
    pid = build(world)
    world.merger.execute(pid)

    assert world.plan(pid).status == "done" and world.canonical() == CANON
    with world.sessions() as s:  # a merge teaches AcoustID nothing
        assert s.scalar(select(Job.id).where(Job.kind == JobKind.ACOUSTID_SUBMIT)) is None
    now = world.tracks()
    canon_dir = "Band/Album (1999)/"
    assert now["r1"] == (canon_dir + "01-01 Song r1.flac", CANON)
    assert now["r2"] == (canon_dir + "01-02 Song r2.flac", CANON)
    assert now["r3"] == (canon_dir + "01-03 Song r3.flac", CANON)  # the canonical copy remains
    assert now["r9"] == old["r9"]  # kept untouched
    assert len(now) == 4
    got = tags.read_fields(world.ops.library / now["r1"][0])
    assert got["musicbrainz_albumid"] == CANON and got["date"] == "1999"
    assert got["acoustid_id"] == "ac-r1" and got["mixsync_status"] == "verified"
    # every original is in the trash, byte for byte
    trash = {f.rpartition("/")[2]: sha(world.ops.trash_dir / f) for f in world.trashed()}
    for rec in ("r1", "r2", "r3"):
        path = old[rec][0]
        assert trash[path.rpartition("/")[2]] == before[path]
    assert not (world.ops.library / old["r1"][0]).exists()
    with world.sessions() as s:
        assert s.scalar(select(Job.id).where(Job.kind == JobKind.RESCAN)) is not None


def test_crash_after_publish_resumes_without_duplicates(world: World) -> None:
    pid = build(world)
    real = fileops._hook  # pyright: ignore[reportPrivateUsage]

    def crash(step: str) -> None:
        if step == "rename":
            raise Boom

    fileops._hook = crash  # pyright: ignore[reportPrivateUsage]
    try:
        with pytest.raises(Boom):
            world.merger.execute(pid)
    finally:
        fileops._hook = real  # pyright: ignore[reportPrivateUsage]
    world.ops.recover()
    world.merger.execute(pid)
    assert_merged_once(world, pid)


def test_crash_between_file_op_and_row_update_resumes(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = build(world)
    real = Merger._commit_op  # pyright: ignore[reportPrivateUsage]
    calls: list[int] = []

    def crash(self: Merger, *a: Any, **k: Any) -> None:
        calls.append(1)
        if len(calls) in (1, 3):  # after a trash, and after a move's copy and trash
            raise Boom
        real(self, *a, **k)

    monkeypatch.setattr(Merger, "_commit_op", crash)
    for _ in range(2):
        with pytest.raises(Boom):
            world.merger.execute(pid)
    world.merger.execute(pid)
    assert_merged_once(world, pid)


def assert_merged_once(w: World, pid: int) -> None:
    assert w.plan(pid).status == "done"
    t = w.tracks()
    assert sorted(p for p, _ in t.values()) == sorted(w.lib()) and len(t) == 4
    assert t["r1"][1] == t["r2"][1] == CANON
    # three originals trashed exactly once each, nothing else
    assert sorted(f.rpartition("/")[2] for f in w.trashed()) == [
        "01-01 Song r1.flac", "01-02 Song r2.flac", "01-03 Song r3.flac",
    ]  # fmt: skip
    assert files(w.ops.incoming) == []
    assert SqlJournal(w.sessions, SystemClock()).pending() == []


def test_revalidation_failure_stops_before_touching_files(world: World) -> None:
    pid = build(world)
    victim = world.tracks()["r2"][0]
    with world.sessions.begin() as s:  # the row no longer matches what the plan saw
        s.scalars(select(Track).where(Track.path == victim)).one().status = "unverified"
    before = world.lib()
    with pytest.raises(SafetyError):
        world.merger.execute(pid)
    plan = world.plan(pid)
    assert plan.status == "failed" and victim in str(plan.error)
    ops = ops_by_kind(world, pid)
    # r3's trash and r1's move ran first; the failing op and the ones after it did not
    assert [o["done"] for o in ops["trash"]] == [True]
    assert [o["done"] for o in ops["move"]] == [True, False]
    assert before[victim] == world.lib()[victim]


def test_trash_op_needs_its_keeper(world: World) -> None:
    pid = build(world)
    keeper = ops_by_kind(world, pid)["trash"][0]["keeper"]
    (world.ops.library / keeper).rename(world.ops.library / "elsewhere.flac")
    with pytest.raises(SafetyError, match="copy to keep"):
        world.merger.execute(pid)
    assert world.trashed() == [] and world.plan(pid).status == "failed"


def test_canonical_changed_underneath_fails_the_plan(world: World) -> None:
    pid = build(world)  # built with no stored canonical
    with world.sessions.begin() as s:
        canonical_db.store(s, GROUP, OLD)  # someone else stored a choice since
    before = world.lib()
    with pytest.raises(SafetyError, match="canonical"):
        world.merger.execute(pid)
    assert world.lib() == before and world.trashed() == [] and world.canonical() == OLD
    assert world.plan(pid).status == "failed"
    assert str(world.plan(pid).error).startswith("blocked: ")


def test_undo_restores_everything(world: World) -> None:
    pid = build(world)
    with world.sessions.begin() as s:  # a switch from OLD, not a first store
        canonical_db.store(s, GROUP, OLD)
        s.get_one(Plan, pid).previous_canonical_mbid = OLD
    paths_before = world.tracks()
    lib_before = world.lib()
    tags_before = {p: tags.read_all(world.ops.library / p) for p in lib_before}
    rows_before = [fragmentation.row_dict(t) for t in _rows(world)]

    world.merger.execute(pid)
    assert world.canonical() == CANON and world.lib() != lib_before
    world.merger.undo_plan(pid)

    assert world.plan(pid).status == "undone" and world.canonical() == OLD
    assert world.lib() == lib_before  # byte-identical, nothing extra in the library
    assert {p: tags.read_all(world.ops.library / p) for p in lib_before} == tags_before
    assert world.tracks() == paths_before
    assert [fragmentation.row_dict(t) for t in _rows(world)] == rows_before
    # the merged copies are in the trash, not deleted
    assert any("Album (1999)" in f for f in world.trashed())
    with world.sessions() as s:
        assert s.scalar(select(FileOp.id).where(FileOp.status == "started")) is None


def _rows(w: World) -> list[Track]:
    with w.sessions() as s:
        return list(s.scalars(select(Track).order_by(Track.id)))


def test_undo_of_a_first_store_removes_the_canonical_row(world: World) -> None:
    pid = build(world)
    world.merger.execute(pid)
    world.merger.undo_plan(pid)
    assert world.canonical() is None


def test_undo_of_a_failed_partial_plan_undoes_only_what_ran(world: World) -> None:
    pid = build(world)
    lib_before = world.lib()
    rows_before = [fragmentation.row_dict(t) for t in _rows(world)]
    victim = world.tracks()["r2"][0]
    with world.sessions.begin() as s:  # fail on r2, after r3's trash and r1's move are done
        s.scalars(select(Track).where(Track.path == victim)).one().acoustid_id = "changed"
    with pytest.raises(SafetyError):
        world.merger.execute(pid)
    assert [o["done"] for o in world.plan(pid).ops].count(True) == 2
    with world.sessions.begin() as s:
        s.scalars(select(Track).where(Track.path == victim)).one().acoustid_id = "ac-r2"
    world.merger.undo_plan(pid)
    assert world.plan(pid).status == "undone" and world.canonical() is None
    assert world.lib() == lib_before
    assert [fragmentation.row_dict(t) for t in _rows(world)] == rows_before


def test_restore_refuses_to_overwrite(world: World) -> None:
    rel = world.tracks()["r1"][0]
    trash_rel = world.ops.trash(rel, "b")
    (world.ops.library / rel).write_bytes(b"someone else")
    with pytest.raises(FileExistsError):
        world.ops.restore(trash_rel, rel, "b")
    assert (world.ops.library / rel).read_bytes() == b"someone else"
    assert (world.ops.trash_dir / trash_rel).exists()


def test_recover_handles_an_interrupted_restore(world: World, journal: SqlJournal) -> None:
    rel = world.tracks()["r1"][0]
    digest = sha(world.ops.library / rel)
    trash_rel = world.ops.trash(rel, "b")

    real = fileops._hook  # pyright: ignore[reportPrivateUsage]

    def crash(step: str) -> None:
        if step == "restore":
            raise Boom

    fileops._hook = crash  # pyright: ignore[reportPrivateUsage]
    try:
        with pytest.raises(Boom):
            world.ops.restore(trash_rel, rel, "b")  # died before the rename
    finally:
        fileops._hook = real  # pyright: ignore[reportPrivateUsage]
    (op,) = journal.pending()
    world.ops.recover()
    assert journal.pending() == [] and not (world.ops.library / rel).exists()
    assert _status(world, op.id) == "rolled_back"

    op_id = journal.begin("b", "restore", trash_rel, rel)  # died after the rename
    os.rename(world.ops.trash_dir / trash_rel, world.ops.library / rel)
    world.ops.recover()
    assert _status(world, op_id) == "done" and sha(world.ops.library / rel) == digest


def _status(w: World, op_id: int) -> str:
    with w.sessions() as s:
        return s.get_one(FileOp, op_id).status


def crash_once(
    monkeypatch: pytest.MonkeyPatch, name: str, nth: int = 1, cls: type = Merger
) -> None:
    """Make the nth call of a Merger (or `cls`) method die like a killed process."""
    real, calls = getattr(cls, name), []

    def crash(self: Any, *a: Any, **k: Any) -> Any:
        calls.append(1)
        if len(calls) == nth:
            raise Boom
        return real(self, *a, **k)

    monkeypatch.setattr(cls, name, crash)


def snapshot(w: World) -> tuple[dict[str, str], list[dict[str, Any]]]:
    return w.lib(), [fragmentation.row_dict(t) for t in _rows(w)]


def test_undo_with_the_old_path_taken_fails_the_plan_and_keeps_the_library(world: World) -> None:
    old = world.tracks()
    pid = build(world)
    world.merger.execute(pid)
    merged = world.tracks()["r1"][0]
    (world.ops.library / old["r1"][0]).write_bytes(b"other")  # someone reuses the old path
    with pytest.raises(FileExistsError):
        world.merger.undo_plan(pid)
    plan = world.plan(pid)
    assert plan.status == "failed" and str(plan.error).startswith("undo:")
    assert (world.ops.library / merged).exists()  # the live merged copy was not trashed
    assert world.tracks()["r1"][0] == merged
    assert (world.ops.library / old["r1"][0]).read_bytes() == b"other"


def test_retry_after_a_crash_leaves_a_newcomer_at_the_vacated_path(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = world.tracks()
    pid = build(world)
    crash_once(monkeypatch, "_commit_op")
    with pytest.raises(Boom):
        world.merger.execute(pid)  # r3's original is trashed, its row not yet updated
    newcomer = world.ops.library / old["r3"][0]
    newcomer.write_bytes(b"newcomer")
    world.merger.execute(pid)
    assert newcomer.read_bytes() == b"newcomer"
    assert world.plan(pid).status == "done"


def test_same_release_folder_is_refiled_in_place_without_a_suffix(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    monkeypatch.setattr(sys.modules[__name__], "CANON_ALBUM", replace(CANON_ALBUM, year=2000))
    old, before = world.tracks(), snapshot(world)
    pid = build(world)
    moves = ops_by_kind(world, pid)["move"]
    assert [m["new_path"] for m in moves] == [m["old"]["path"] for m in moves]  # no " (2)"
    world.merger.execute(pid)
    assert world.tracks()["r1"] == (old["r1"][0], CANON)
    assert tags.read_fields(world.ops.library / old["r1"][0])["musicbrainz_albumid"] == CANON
    world.merger.undo_plan(pid)
    assert snapshot(world) == before


def test_a_move_onto_an_occupied_path_fails_before_trashing(world: World) -> None:
    pid = build(world)
    target = ops_by_kind(world, pid)["move"][1]["new_path"]
    (world.ops.library / target).write_bytes(b"mine")
    with pytest.raises(SafetyError, match="already exists"):
        world.merger.execute(pid)
    assert (world.ops.library / target).read_bytes() == b"mine"
    assert world.tracks()["r2"][0] != target
    assert (world.ops.library / world.tracks()["r2"][0]).exists()
    assert not any("r2" in f for f in world.trashed())


def test_build_avoids_paths_that_only_have_a_row(world: World) -> None:
    ghost = "Band/Album (1999)/01-01 Song r1.flac"
    with world.sessions.begin() as s:
        s.add(Track(path=ghost, recording_mbid="x", release_mbid="z", release_group_mbid="other",
                    status="verified", batch_id="b", imported_at=SystemClock().now()))  # fmt: skip
    pid = build(world)
    assert ops_by_kind(world, pid)["move"][0]["new_path"] == ghost.replace(".flac", " (2).flac")


def test_one_copy_per_recording(world: World) -> None:
    extra = world.importer.import_release(
        [(world.ops.library / world.tracks()["r1"][0], OLD_ALBUM.tracks[0], "ac-r1")],
        replace(OLD_ALBUM, release_id="old2", year=2001),
        "verified",
        "seed2",
    )
    assert extra[0].rel
    pid = build(world)
    kinds = ops_by_kind(world, pid)
    dup = next(o for o in kinds["trash"] if o["old"]["release_mbid"] == "old2")
    first = kinds["move"][0]
    assert first["old"]["recording_mbid"] == "r1" and dup["keeper"] == first["new_path"]
    assert [o["old"]["recording_mbid"] for o in kinds["keep"]] == ["r9"]  # not on the canonical
    world.merger.execute(pid)
    assert len([t for t in _rows(world) if t.recording_mbid == "r1"]) == 1
    assert len([f for f in world.lib() if "r1" in f]) == 1


def test_recordings_without_an_mbid_are_not_matched(world: World) -> None:
    def row(i: int, release: str) -> Track:
        return Track(id=i, path=str(i), recording_mbid=None, release_mbid=release, artist_mbids=[],
                     release_group_mbid=GROUP, status="verified", batch_id="b",
                     imported_at=SystemClock().now())  # fmt: skip

    rows = [row(1, CANON), row(2, OLD)]
    ops = fragmentation.build_ops(rows, CANON_ALBUM, world.merger.template, world.ops.has)
    assert [o["op"] for o in ops] == ["keep"]


def test_canonical_is_rechecked_before_each_op(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = build(world)
    real = Merger._commit_op  # pyright: ignore[reportPrivateUsage]

    def commit_then_switch(self: Merger, *a: Any, **k: Any) -> None:
        real(self, *a, **k)
        with world.sessions.begin() as s:
            s.get_one(CanonicalRelease, GROUP).release_mbid = "elsewhere"

    monkeypatch.setattr(Merger, "_commit_op", commit_then_switch)
    with pytest.raises(SafetyError, match="canonical"):
        world.merger.execute(pid)
    assert [o["done"] for o in world.plan(pid).ops].count(True) == 1
    assert world.canonical() == "elsewhere"


def test_any_error_fails_the_plan_and_it_can_be_requeued(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:

    pid = build(world)
    monkeypatch.setattr(FileOps, "trash", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        world.merger.execute(pid)
    assert world.plan(pid).status == "failed" and "disk" in str(world.plan(pid).error)
    monkeypatch.undo()
    jobs = []
    for _ in range(2):  # the double click is one job; the failure made the second a new one
        with world.sessions.begin() as s:
            enqueue_merge(s, SystemClock().now(), pid)
            enqueue_merge(s, SystemClock().now(), pid)
        with world.sessions() as s:
            jobs.append(len(s.scalars(select(Job).where(Job.kind == JobKind.MERGE)).all()))
        if len(jobs) == 1:
            with world.sessions.begin() as s:
                s.get_one(Plan, pid).error = "again"
    assert jobs == [1, 2]
    world.merger.execute(pid)
    assert world.plan(pid).status == "done"


def test_undo_resumes_after_a_crash_between_restore_and_trashing_the_copy(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = snapshot(world)
    pid = build(world)
    world.merger.execute(pid)
    crash_once(monkeypatch, "trash", cls=FileOps)
    with pytest.raises(Boom):
        world.merger.undo_plan(pid)
    assert world.plan(pid).status == "undoing"
    world.merger.undo_plan(pid)
    assert world.plan(pid).status == "undone" and snapshot(world) == before


def test_undo_resumes_after_a_crash_between_file_ops_and_the_row_update(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = snapshot(world)
    pid = build(world)
    world.merger.execute(pid)
    crash_once(monkeypatch, "_commit_op", 2)  # the second op, files restored, rows not yet
    with pytest.raises(Boom):
        world.merger.undo_plan(pid)
    world.merger.undo_plan(pid)
    assert world.plan(pid).status == "undone" and snapshot(world) == before
    assert world.canonical() is None


def test_undo_reverses_an_op_that_was_half_done(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = snapshot(world)
    pid = build(world)
    crash_once(monkeypatch, "_commit_op", 2)  # r1: original trashed and copied, row not updated
    with pytest.raises(Boom):
        world.merger.execute(pid)
    assert world.plan(pid).status == "running"
    monkeypatch.undo()
    world.merger.undo_plan(pid)
    assert world.plan(pid).status == "undone" and snapshot(world) == before


def test_build_discards_an_abandoned_draft(world: World) -> None:
    first = build(world)
    second = build(world)
    assert first != second
    assert world.plan(first).status == "discarded" and world.plan(second).status == "draft"
    for run in (world.merger.execute, world.merger.undo_plan):
        with pytest.raises(SafetyError, match="discarded"):
            run(first)
    assert world.trashed() == []


@pytest.mark.parametrize("status", ["running", "undoing"])
def test_build_refuses_while_a_plan_is_active(world: World, status: str) -> None:
    pid = build(world)
    with world.sessions.begin() as s:
        s.get_one(Plan, pid).status = status
    with pytest.raises(PlanInProgress):
        build(world)
    assert world.plan(pid).status == status


def test_undoing_a_plan_that_set_nothing_leaves_the_canonical_alone(world: World) -> None:
    p1 = build(world)
    with world.sessions.begin() as s:
        s.get_one(Plan, p1).status = "failed"  # e.g. it failed on a stale row before doing anything
    p2 = build(world)
    world.merger.execute(p2)
    assert world.canonical() == CANON
    world.merger.undo_plan(p1)
    assert world.plan(p1).status == "undone" and world.canonical() == CANON


def test_undo_after_the_user_retagged_the_merged_file(world: World) -> None:
    import mutagen

    before = snapshot(world)
    pid = build(world)
    world.merger.execute(pid)
    f = world.ops.library / world.tracks()["r1"][0]
    m = mutagen.File(f)  # pyright: ignore[reportUnknownMemberType]
    m["title"] = ["edited"]
    m.save()
    world.merger.undo_plan(pid)
    assert world.plan(pid).status == "undone" and snapshot(world) == before
    assert any("edited" not in f and "01-01" in f for f in world.trashed())  # kept in .trash


def test_undo_leaves_a_file_that_is_not_the_merged_copy(world: World) -> None:
    pid = build(world)
    world.merger.execute(pid)
    row = _rows(world)
    r1 = next(t for t in row if t.recording_mbid == "r1")
    (world.ops.library / r1.path).write_bytes(b"not ours")
    with world.sessions.begin() as s:  # and the library no longer lists it at that path
        s.get_one(Track, r1.id).path = ops_by_kind(world, pid)["move"][0]["old"]["path"]
    after = snapshot(world)
    with pytest.raises(SafetyError, match="not the merged copy"):
        world.merger.undo_plan(pid)
    assert world.plan(pid).status == "failed"
    assert (world.ops.library / r1.path).read_bytes() == b"not ours"
    assert snapshot(world)[0].get(r1.path) == after[0][r1.path]


def test_the_bigger_copy_is_the_one_kept(world: World) -> None:
    (extra,) = world.importer.import_release(
        [(world.ops.library / world.tracks()["r1"][0], OLD_ALBUM.tracks[0], "ac-r1")],
        replace(OLD_ALBUM, release_id="old2", year=2001),
        "verified",
        "seed2",
    )
    assert extra.rel
    with (world.ops.library / extra.rel).open("ab") as f:
        f.write(b"\0" * 5000)  # the 2001 copy is the larger one
    pid = build(world)
    kinds = ops_by_kind(world, pid)
    moved = next(o for o in kinds["move"] if o["old"]["recording_mbid"] == "r1")
    dup = next(o for o in kinds["trash"] if o["old"]["recording_mbid"] == "r1")
    assert moved["old"]["path"] == extra.rel and dup["keeper"] == moved["new_path"]


def test_same_path_undo_resumes_after_the_copy_is_trashed_but_not_restored(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    monkeypatch.setattr(sys.modules[__name__], "CANON_ALBUM", replace(CANON_ALBUM, year=2000))
    before = snapshot(world)
    pid = build(world)
    world.merger.execute(pid)

    def crash(step: str) -> None:
        if step == "restore":
            raise Boom

    real = fileops._hook  # pyright: ignore[reportPrivateUsage]
    fileops._hook = crash  # pyright: ignore[reportPrivateUsage]
    try:
        with pytest.raises(Boom):
            world.merger.undo_plan(pid)
    finally:
        fileops._hook = real  # pyright: ignore[reportPrivateUsage]
    world.ops.recover()
    world.merger.undo_plan(pid)
    assert world.plan(pid).status == "undone" and snapshot(world) == before


def test_a_confirmed_draft_is_not_discarded_by_a_new_preview(world: World) -> None:
    pid = build(world)
    with world.sessions.begin() as s:
        enqueue_merge(s, SystemClock().now(), pid)  # confirmed; the worker has not run it yet
    with world.sessions() as s:
        assert live_job(s, pid)
    with pytest.raises(PlanInProgress):
        build(world)
    assert world.plan(pid).status == "draft"
    world.merger.execute(pid)  # the confirmed merge still runs
    assert world.plan(pid).status == "done"
    with world.sessions() as s:
        assert not live_job(s, pid + 1000)


def test_merge_and_undo_requests_share_one_job_per_plan_state(world: World) -> None:
    pid = build(world)
    with world.sessions.begin() as s:
        enqueue_merge(s, SystemClock().now(), pid)
    with world.sessions.begin() as s:
        enqueue_undo(s, SystemClock().now(), pid)
    with world.sessions() as s:
        kinds = [j.kind for j in s.scalars(select(Job).where(Job.kind != JobKind.RESCAN))]
    assert kinds == [JobKind.MERGE]


def test_undo_window_closes_and_the_trash_is_then_purged(world: World) -> None:
    pid = build(world)
    world.merger.execute(pid)
    before = world.lib()
    assert world.trashed()
    purger = TrashPurger(world.sessions, SystemClock(), world.ops, 30)
    assert purger.protected()  # inside the window: undo works and its trash is kept
    with world.sessions.begin() as s:  # finished 31 days ago
        s.get_one(Plan, pid).updated_at = datetime.now(UTC) - timedelta(days=31)
    assert purger.protected() == set()
    with pytest.raises(SafetyError, match="undo window of 30 days has passed"):
        world.merger.undo_plan(pid)
    assert world.plan(pid).status == "done" and world.lib() == before


def test_undoable_window_edge_is_one_rule() -> None:
    now = datetime(2026, 10, 11, tzinfo=UTC)

    def plan(status: str, age: timedelta) -> Plan:
        return Plan(status=status, updated_at=now - age)

    day = timedelta(days=1)
    assert undoable(plan("done", 30 * day - timedelta(seconds=1)), now, 30)
    assert not undoable(plan("done", 30 * day), now, 30)  # exactly at the edge: hidden and purged
    assert undoable(plan("failed", 99 * day), now, 30)  # may hold the only copy: never windowed
    assert undoable(plan("running", 99 * day), now, 30)
    assert undoable(plan("undoing", 99 * day), now, 30)
    assert not undoable(plan("undone", day), now, 30)


def test_an_aware_non_utc_timestamp_is_compared_as_the_instant_it_is() -> None:
    now = datetime(2026, 10, 11, 12, tzinfo=UTC)
    east = timezone(timedelta(hours=14))
    old = datetime(2026, 9, 11, 13, tzinfo=east)  # 2026-09-10 23:00Z: 31 days before now
    recent = datetime(2026, 9, 13, 1, tzinfo=east)  # 2026-09-12 11:00Z: 29 days before now
    assert not undoable(Plan(status="done", updated_at=old), now, 30)
    assert undoable(Plan(status="done", updated_at=recent), now, 30)


def test_undo_refuses_before_moving_anything_when_the_trashed_original_is_gone(
    world: World,
) -> None:
    pid = build(world)
    world.merger.execute(pid)
    lib = world.lib()
    for rel in world.trashed():
        (world.ops.trash_dir / rel).unlink()
    with pytest.raises(SafetyError, match="is gone"):
        world.merger.undo_plan(pid)
    assert world.lib() == lib and world.plan(pid).status == "failed"


def test_undo_refuses_up_front_when_only_some_trashed_originals_are_gone(world: World) -> None:
    pid = build(world)
    world.merger.execute(pid)
    lib = world.lib()
    op = world.plan(pid).ops[0]
    with world.sessions() as s:
        trashed = s.scalars(
            select(FileOp).where(FileOp.kind == "trash", FileOp.src == op["old"]["path"])
        ).one()
    (world.ops.trash_dir / trashed.dst).unlink()
    with pytest.raises(SafetyError, match="is gone"):
        world.merger.undo_plan(pid)
    assert world.lib() == lib and world.plan(pid).status == "failed"


def test_import_after_a_failed_move_does_not_take_the_trashed_path_so_undo_works(
    world: World, make_src: MakeSrc, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = world.tracks()
    pid = build(world)
    crash_once(monkeypatch, "_commit_op")
    with pytest.raises(Boom):
        world.merger.execute(pid)  # r3's original is trashed, its row still points at it
    assert not (world.ops.library / old["r3"][0]).exists()
    other = world.imp(make_src("flac", "again"), OLD, "r3", "later")
    assert other == old["r3"][0].replace(".flac", " (2).flac")
    world.merger.undo_plan(pid)
    assert world.plan(pid).status == "undone"
    assert (world.ops.library / old["r3"][0]).exists()
