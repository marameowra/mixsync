import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.jobs import JobKind
from mixsync.db.journal import SqlJournal
from mixsync.db.models.library import Plan
from mixsync.db.models.safety import FileOp
from mixsync.db.models.work import Job
from mixsync.db.queue import JobRecord
from mixsync.library.fileops import FileOps
from mixsync.library.purge import INTERVAL, TrashPurger

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.t = NOW

    def now(self) -> datetime:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def fo(data_dir: Path, sessions: sessionmaker[Session], clock: Clock) -> FileOps:
    return FileOps(data_dir, data_dir.parent / "dl", SqlJournal(sessions, clock), clock)


def day(n: int) -> str:
    return (NOW.date() - timedelta(days=n)).isoformat()


def put(fo: FileOps, rel: str, data: bytes = b"x") -> Path:
    p = fo.trash_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def ops(sessions: sessionmaker[Session]) -> list[FileOp]:
    with sessions() as s:
        return list(s.scalars(select(FileOp).order_by(FileOp.id)))


def test_old_trash_of_both_roots_is_purged_and_journaled_newer_kept(
    fo: FileOps, sessions: sessionmaker[Session]
) -> None:
    old_d = put(fo, f"{day(40)}/.discover/a/d.mp3")
    old_l = put(fo, f"{day(40)}/Artist/l.mp3")
    new_d = put(fo, f"{day(5)}/.discover/n.mp3", b"nd")
    new_l = put(fo, f"{day(5)}/n.mp3", b"nl")
    assert fo.purge_trash(30, "purge-x") == 2
    assert not old_d.exists() and not old_l.exists()
    assert new_d.read_bytes() == b"nd" and new_l.read_bytes() == b"nl"
    assert not (fo.trash_dir / day(40)).exists()  # emptied folders go
    got = sorted((o.kind, o.root, o.src, o.status, o.batch_id) for o in ops(sessions))
    assert got == [
        ("purge", "discover", f"{day(40)}/.discover/a/d.mp3", "done", "purge-x"),
        ("purge", "library", f"{day(40)}/Artist/l.mp3", "done", "purge-x"),
    ]
    assert fo.purge_trash(30, "purge-y") == 0  # idempotent
    assert len(ops(sessions)) == 2


def test_cutoff_exactly_n_days_is_kept_n_plus_one_is_purged(fo: FileOps) -> None:
    # "older than 30 days" is strict: a folder 30 days old has not yet been there longer than that
    at = put(fo, f"{day(30)}/.discover/a.mp3")
    past = put(fo, f"{day(31)}/.discover/b.mp3")
    assert fo.purge_trash(30, "b") == 1
    assert at.exists() and not past.exists()


def test_symlinks_are_skipped_and_targets_untouched(fo: FileOps, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.mp3").write_bytes(b"precious")
    d = fo.trash_dir / day(40) / ".discover"
    d.mkdir(parents=True)
    (d / "link.mp3").symlink_to(outside / "keep.mp3")
    (d / "linkdir").symlink_to(outside)
    assert fo.purge_trash(30, "b") == 0
    assert (d / "link.mp3").is_symlink() and (d / "linkdir").is_symlink()
    assert (outside / "keep.mp3").read_bytes() == b"precious"


def test_bad_day_names_are_skipped(fo: FileOps) -> None:
    keep = [
        put(fo, "notaday/.discover/a.mp3"),
        put(fo, "2026-13-45/a.mp3"),
        put(fo, "20200101/a.mp3"),
    ]
    assert fo.purge_trash(30, "b") == 0
    assert all(p.exists() for p in keep)


def test_recover_finishes_a_started_purge(fo: FileOps, sessions: sessionmaker[Session]) -> None:
    journal = SqlJournal(sessions, Clock())
    left = put(fo, f"{day(40)}/.discover/a.mp3")
    gone = f"{day(40)}/.discover/gone.mp3"
    journal.begin("b", "purge", f"{day(40)}/.discover/a.mp3", "", root="discover")
    journal.begin("b", "purge", gone, "", root="discover")
    fo.recover()
    assert not left.exists()
    assert [o.status for o in ops(sessions)] == ["done", "done"]


def test_recover_will_not_purge_outside_a_dated_folder(
    fo: FileOps, sessions: sessionmaker[Session]
) -> None:
    other = put(fo, ".incoming/1")
    SqlJournal(sessions, Clock()).begin("b", "purge", ".incoming/1", "", root="library")
    with pytest.raises(Exception, match="dated trash folder"):
        fo.recover()
    assert other.exists()


def _plan(sessions: sessionmaker[Session], status: str) -> None:
    with sessions.begin() as s:
        s.add(
            Plan(
                kind="merge",
                release_group_mbid="g",
                canonical_release_mbid="r",
                batch_id="merge-1",
                status=status,
            )
        )


def test_trash_needed_by_an_undoable_plan_survives_until_it_is_undone(
    fo: FileOps, sessions: sessionmaker[Session], clock: Clock
) -> None:
    src = fo.library / "A/b.mp3"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"orig")
    clock.t = NOW - timedelta(days=40)  # trashed long ago
    rel = fo.trash("A/b.mp3", "merge-1")
    clock.t = NOW
    _plan(sessions, "done")
    purger = TrashPurger(sessions, clock, fo, 30)
    assert purger.protected() == {rel}
    asyncio.run(purger.purge(_job(1)))
    assert (fo.trash_dir / rel).read_bytes() == b"orig"
    with sessions.begin() as s:
        s.execute(select(Plan)).scalar_one().status = "undone"
    asyncio.run(purger.purge(_job(2)))
    assert not (fo.trash_dir / rel).exists()


def test_trash_of_a_plan_past_its_undo_window_is_purged(
    fo: FileOps, sessions: sessionmaker[Session], clock: Clock
) -> None:
    src = fo.library / "A/b.mp3"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"orig")
    clock.t = NOW - timedelta(days=40)
    rel = fo.trash("A/b.mp3", "merge-1")
    clock.t = NOW
    _plan(sessions, "done")
    purger = TrashPurger(sessions, clock, fo, 30)
    with sessions.begin() as s:
        s.execute(select(Plan)).scalar_one().updated_at = NOW - timedelta(days=30)
    assert purger.protected()  # undo's edge, inside the purge's margin
    with sessions.begin() as s:
        s.execute(select(Plan)).scalar_one().updated_at = NOW - timedelta(days=31)
    assert purger.protected() == set()
    asyncio.run(purger.purge(_job(1)))
    assert not (fo.trash_dir / rel).exists()


def _job(i: int) -> JobRecord:
    return JobRecord(
        id=i,
        kind=JobKind.TRASH_PURGE,
        payload={},
        status="running",  # type: ignore[arg-type]
        attempts=1,
        max_attempts=5,
        locked_by=None,
        run_after=NOW,
        lease_expires_at=None,
        last_error=None,
    )


def test_chain_starts_once_and_reschedules_about_a_day_later(
    fo: FileOps, sessions: sessionmaker[Session], clock: Clock
) -> None:
    purger = TrashPurger(sessions, clock, fo, 30, rng=lambda lo, hi: lo)
    purger.start_chains()
    purger.start_chains()  # a restart adds no second chain
    with sessions() as s:
        (first,) = s.scalars(select(Job).where(Job.kind == JobKind.TRASH_PURGE))
        fid = first.id
    asyncio.run(purger.purge(_job(fid)))
    asyncio.run(purger.purge(_job(fid)))  # a retry adds no second continuation
    with sessions() as s:
        jobs = list(s.scalars(select(Job).where(Job.kind == JobKind.TRASH_PURGE).order_by(Job.id)))
    assert len(jobs) == 2
    delay = jobs[1].run_after.replace(tzinfo=UTC) - NOW
    assert delay == INTERVAL * 0.75
    purger2 = TrashPurger(sessions, clock, fo, 30, rng=lambda lo, hi: hi)
    asyncio.run(purger2.purge(_job(99)))
    with sessions() as s:
        last = s.scalars(select(Job).order_by(Job.id.desc())).first()
        assert last and last.run_after.replace(tzinfo=UTC) - NOW == INTERVAL * 1.25
    purger.start_chains()  # a live chain exists
    with sessions() as s:
        assert len(list(s.scalars(select(Job).where(Job.kind == JobKind.TRASH_PURGE)))) == 3
    assert os.path.isdir(fo.library)


def test_a_symlinked_trash_dir_is_never_purged_through(fo: FileOps, tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    old = real / day(40) / "a.mp3"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"x")
    fo.trash_dir.symlink_to(real)
    assert fo.purge_trash(30, "b") == 0
    assert old.read_bytes() == b"x"


def test_a_symlinked_day_folder_is_skipped(fo: FileOps, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "x.mp3").parent.mkdir()
    (outside / "x.mp3").write_bytes(b"x")
    fo.trash_dir.mkdir()
    (fo.trash_dir / day(40)).symlink_to(outside)
    assert fo.purge_trash(30, "b") == 0
    assert (outside / "x.mp3").exists()


def test_a_future_dated_folder_is_kept(fo: FileOps) -> None:
    p = put(fo, f"{day(-3)}/a.mp3")
    assert fo.purge_trash(30, "b") == 0 and p.exists()


@pytest.mark.parametrize("rel", ["../outside.mp3", "ABSOLUTE", "plain.mp3", "2026-W01-1/a.mp3"])
def test_recover_rejects_rows_outside_dated_folders(
    fo: FileOps, sessions: sessionmaker[Session], tmp_path: Path, rel: str
) -> None:
    rel = str(tmp_path / "abs.mp3") if rel == "ABSOLUTE" else rel
    target = Path(rel) if rel.startswith("/") else fo.trash_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()
    SqlJournal(sessions, Clock()).begin("b", "purge", rel, "", root="library")
    with pytest.raises(Exception, match="dated trash folder"):
        fo.recover()
    assert target.exists()


@pytest.mark.parametrize("status", ["running", "undoing", "failed", "done"])
def test_protection_covers_every_undoable_status(
    fo: FileOps, sessions: sessionmaker[Session], clock: Clock, status: str
) -> None:
    (fo.library / "A").mkdir()
    (fo.library / "A/b.mp3").write_bytes(b"orig")
    rel = fo.trash("A/b.mp3", "merge-1")
    _plan(sessions, status)
    with sessions.begin() as s:  # long finished for done/failed, but still inside the window
        s.execute(select(Plan)).scalar_one().updated_at = NOW - timedelta(days=29)
    assert TrashPurger(sessions, clock, fo, 30).protected() == {rel}


def test_a_protected_discover_entry_survives_and_odd_spellings_still_match(fo: FileOps) -> None:
    nfd = put(fo, f"{day(40)}/.discover/Beyoncé/a.mp3")  # as the file system spells it
    other = put(fo, f"{day(40)}/.discover/b.mp3")
    keep = [f"{day(40)}//.discover/Beyoncé/a.mp3"]  # NFC with a double slash
    assert fo.purge_trash(30, "b", keep) == 1
    assert nfd.exists() and not other.exists()


def _trashed_long_ago(fo: FileOps, clock: Clock) -> str:
    (fo.library / "A").mkdir(exist_ok=True)
    (fo.library / "A/b.mp3").write_bytes(b"only copy")
    clock.t = NOW - timedelta(days=40)
    rel = fo.trash("A/b.mp3", "merge-1")
    clock.t = NOW
    return rel


def test_a_failed_plan_past_the_window_keeps_its_only_copy(
    fo: FileOps, sessions: sessionmaker[Session], clock: Clock
) -> None:
    rel = _trashed_long_ago(fo, clock)
    _plan(sessions, "failed")
    with sessions.begin() as s:
        s.execute(select(Plan)).scalar_one().updated_at = NOW - timedelta(days=90)
    asyncio.run(TrashPurger(sessions, clock, fo, 30).purge(_job(1)))
    assert (fo.trash_dir / rel).read_bytes() == b"only copy"


def test_a_not_done_op_keeps_its_trashed_original_even_in_an_old_done_plan(
    fo: FileOps, sessions: sessionmaker[Session], clock: Clock
) -> None:
    rel = _trashed_long_ago(fo, clock)
    _plan(sessions, "done")
    with sessions.begin() as s:
        p = s.execute(select(Plan)).scalar_one()
        p.updated_at = NOW - timedelta(days=90)
        p.ops = [{"op": "move", "old": {"path": "A/b.mp3"}, "done": False}]
    asyncio.run(TrashPurger(sessions, clock, fo, 30).purge(_job(1)))
    assert (fo.trash_dir / rel).exists()


def test_a_file_that_cannot_be_deleted_is_rolled_back_not_counted(
    fo: FileOps, sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    p = put(fo, f"{day(40)}/a.mp3")
    monkeypatch.setattr(fo, "_purge_file", lambda _rel: False)
    assert fo.purge_trash(30, "b") == 0
    assert p.exists() and [o.status for o in ops(sessions)] == ["rolled_back"]
