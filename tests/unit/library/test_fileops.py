import errno
import hashlib
import re
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import SystemClock
from mixsync.core.errors import SafetyError
from mixsync.db.journal import SqlJournal
from mixsync.db.models.safety import FileOp
from mixsync.library import fileops
from mixsync.library.fileops import FileOps

from .conftest import MakeSrc

REL = "Artist/Album (2000)/01-01 Song.flac"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def noop_tags(_path: Path, _op_id: int) -> None:
    return None


def append_tag(path: Path, _op_id: int) -> None:
    with path.open("ab") as f:  # stands in for a tag write: changes the bytes
        f.write(b"tagged")


def files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def statuses(sessions: sessionmaker[Session]) -> list[str]:
    with sessions() as s:
        return [o.status for o in s.scalars(select(FileOp).order_by(FileOp.id))]


def test_happy_path(
    ops: FileOps,
    data_dir: Path,
    make_src: MakeSrc,
    journal: SqlJournal,
    sessions: sessionmaker[Session],
) -> None:
    src = make_src()
    before = sha(src)
    digest = ops.import_file(src, REL, append_tag, "b1")
    final = data_dir / "library" / REL
    assert digest == sha(final) != before
    assert sha(src) == before
    assert files(data_dir / ".incoming") == []
    assert journal.pending() == []
    assert statuses(sessions) == ["done"]


def test_destination_exists_aborts(
    ops: FileOps, data_dir: Path, make_src: MakeSrc, sessions: sessionmaker[Session]
) -> None:
    final = data_dir / "library" / REL
    final.parent.mkdir(parents=True)
    final.write_bytes(b"precious")
    src = make_src()
    with pytest.raises(FileExistsError):
        ops.import_file(src, REL, append_tag, "b1")
    assert final.read_bytes() == b"precious"
    assert src.exists() and files(data_dir / ".incoming") == []
    assert statuses(sessions) == ["failed"]
    assert re.search(r"\.incoming/\d+$", files(data_dir / ".trash")[0])  # leftover kept, not lost


def test_destination_exists_aborts_on_link_fallback(
    ops: FileOps, data_dir: Path, make_src: MakeSrc, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fileops, "_noreplace", lambda _s, _d: False)
    final = data_dir / "library" / REL
    final.parent.mkdir(parents=True)
    final.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        ops.import_file(make_src(), REL, noop_tags, "b1")
    assert final.read_bytes() == b"precious"


def test_link_fallback_happy_path(
    ops: FileOps, data_dir: Path, make_src: MakeSrc, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fileops, "_noreplace", lambda _s, _d: False)
    src = make_src()
    ops.import_file(src, REL, noop_tags, "b1")
    assert sha(data_dir / "library" / REL) == sha(src)
    assert files(data_dir / ".incoming") == []


def test_hash_mismatch_fails_and_leaves_source(
    ops: FileOps,
    data_dir: Path,
    make_src: MakeSrc,
    monkeypatch: pytest.MonkeyPatch,
    sessions: sessionmaker[Session],
) -> None:
    real = fileops._sha256
    monkeypatch.setattr(
        fileops, "_sha256", lambda p: "bad" if p.parent.name == ".incoming" else real(p)
    )
    src = make_src()
    before = src.read_bytes()
    with pytest.raises(SafetyError, match="hash mismatch"):
        ops.import_file(src, REL, noop_tags, "b1")
    assert src.read_bytes() == before
    assert files(data_dir / "library") == [] and files(data_dir / ".incoming") == []
    assert statuses(sessions) == ["failed"]


def test_source_changed_during_copy_fails_and_keeps_source(
    ops: FileOps,
    data_dir: Path,
    make_src: MakeSrc,
    monkeypatch: pytest.MonkeyPatch,
    sessions: sessionmaker[Session],
) -> None:
    src = make_src()
    real = fileops._hook

    def grow(step: str) -> None:
        if step == "copy":  # a download still being written
            with src.open("ab") as f:
                f.write(b"more bytes arrived")
        real(step)

    monkeypatch.setattr(fileops, "_hook", grow)
    with pytest.raises(SafetyError, match="hash mismatch"):
        ops.import_file(src, REL, noop_tags, "b1")
    assert src.read_bytes().endswith(b"more bytes arrived")
    assert files(data_dir / "library") == [] and files(data_dir / ".incoming") == []
    assert statuses(sessions) == ["failed"]


def test_exdev_is_a_safety_error_not_a_copy_fallback(
    ops: FileOps, data_dir: Path, make_src: MakeSrc, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cross_device(_s: Path, _d: Path) -> bool:
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr(fileops, "_noreplace", cross_device)
    src = make_src()
    with pytest.raises(SafetyError):
        ops.import_file(src, REL, noop_tags, "b1")
    assert src.exists() and files(data_dir / "library") == []


@pytest.mark.parametrize("rel", ["../escape.flac", "/abs.flac", ""])
def test_rejects_paths_outside_library(ops: FileOps, make_src: MakeSrc, rel: str) -> None:
    with pytest.raises(SafetyError):
        ops.import_file(make_src(), rel, noop_tags, "b1")


def test_trash(ops: FileOps, data_dir: Path, make_src: MakeSrc) -> None:
    src = make_src()
    ops.import_file(src, REL, noop_tags, "b1")
    ops.import_file(make_src(name="two"), "x/two.flac", noop_tags, "b1")
    t1 = ops.trash(REL, "b2")
    assert re.fullmatch(r"\d{4}-\d\d-\d\d/" + re.escape(REL), t1)
    assert sha(data_dir / ".trash" / t1) == sha(src)
    assert files(data_dir / "library") == ["x/two.flac"]
    # same path trashed again the same day gets a suffix instead of overwriting
    ops.import_file(src, REL, noop_tags, "b1")
    t2 = ops.trash(REL, "b2")
    assert t2 != t1 and (data_dir / ".trash" / t1).exists()


# --- recover() ---


def start(journal: SqlJournal, kind: str = "copy", src: str = "s", dst: str = REL) -> int:
    return journal.begin("b", kind, src, dst)


def test_recover_completes_op_whose_file_was_renamed(
    ops: FileOps, data_dir: Path, journal: SqlJournal, sessions: sessionmaker[Session]
) -> None:
    op = start(journal)
    final = data_dir / "library" / REL
    final.parent.mkdir(parents=True)
    final.write_bytes(b"audio")
    journal.set_dst_hash(op, hashlib.sha256(b"audio").hexdigest())
    ops.recover()
    assert statuses(sessions) == ["done"] and final.read_bytes() == b"audio"


def test_recover_rolls_back_staging_leftover_into_trash(
    ops: FileOps, data_dir: Path, journal: SqlJournal, sessions: sessionmaker[Session]
) -> None:
    op = start(journal)
    (data_dir / ".incoming").mkdir()
    (data_dir / ".incoming" / str(op)).write_bytes(b"half")
    ops.recover()
    assert statuses(sessions) == ["rolled_back"]
    assert files(data_dir / ".incoming") == [] and files(data_dir / "library") == []
    (kept,) = files(data_dir / ".trash")
    assert kept.endswith(f".incoming/{op}")
    assert (data_dir / ".trash" / kept).read_bytes() == b"half"


def test_recover_rolls_back_hashed_but_unrenamed_and_nothing_started(
    ops: FileOps, data_dir: Path, journal: SqlJournal, sessions: sessionmaker[Session]
) -> None:
    a = start(journal)
    (data_dir / ".incoming").mkdir()
    (data_dir / ".incoming" / str(a)).write_bytes(b"x")
    journal.set_dst_hash(a, "h")
    start(journal, dst="other.flac")  # crashed right after begin: nothing on disk
    ops.recover()
    assert statuses(sessions) == ["rolled_back", "rolled_back"]


def test_recover_never_adopts_someone_elses_file(
    ops: FileOps, data_dir: Path, journal: SqlJournal, sessions: sessionmaker[Session]
) -> None:
    op = start(journal)
    final = data_dir / "library" / REL
    final.parent.mkdir(parents=True)
    final.write_bytes(b"theirs")
    journal.set_dst_hash(op, hashlib.sha256(b"ours").hexdigest())
    ops.recover()
    assert statuses(sessions) == ["rolled_back"] and final.read_bytes() == b"theirs"


def test_recover_trash_ops(
    ops: FileOps, data_dir: Path, journal: SqlJournal, sessions: sessionmaker[Session]
) -> None:
    moved = start(journal, "trash", "a.flac", "2026-01-01/a.flac")  # rename happened
    (data_dir / ".trash/2026-01-01").mkdir(parents=True)
    (data_dir / ".trash/2026-01-01/a.flac").write_bytes(b"a")
    kept = start(journal, "trash", "b.flac", "2026-01-01/b.flac")  # rename never happened
    (data_dir / "library/b.flac").write_bytes(b"b")
    ops.recover()
    assert statuses(sessions) == ["done", "rolled_back"]
    assert moved < kept and (data_dir / "library/b.flac").read_bytes() == b"b"
    assert journal.pending() == []


# --- discover root ---


def test_discover_import_then_trash(ops: FileOps, data_dir: Path, make_src: MakeSrc) -> None:
    src = make_src()
    ops.import_file(src, REL, noop_tags, "b1", root="discover")
    assert files(data_dir / "discover") == [REL] and files(data_dir / "library") == []
    assert ops.has(REL, root="discover") and not ops.has(REL)
    assert ops.find_imported("b1", src, root="discover") == REL
    assert ops.find_imported("b1", src) is None
    t = ops.trash(REL, "b2", root="discover")
    assert re.fullmatch(r"\d{4}-\d\d-\d\d/\.discover/" + re.escape(REL), t)
    assert sha(data_dir / ".trash" / t) == sha(src)
    assert files(data_dir / "discover") == []


def test_write_new_in_discover(ops: FileOps, data_dir: Path) -> None:
    ops.write_new("a/cover.jpg", b"img", "b1", root="discover")
    assert files(data_dir / "discover") == ["a/cover.jpg"] and files(data_dir / "library") == []


def test_recover_pending_discover_copy_and_trash(
    ops: FileOps, data_dir: Path, journal: SqlJournal, sessions: sessionmaker[Session]
) -> None:
    c = journal.begin("b", "copy", "s", REL, root="discover")
    final = data_dir / "discover" / REL
    final.parent.mkdir(parents=True)
    final.write_bytes(b"audio")
    journal.set_dst_hash(c, hashlib.sha256(b"audio").hexdigest())
    journal.begin("b", "trash", "b.flac", "2026-01-01/.discover/b.flac", root="discover")
    (data_dir / ".trash/2026-01-01/.discover").mkdir(parents=True)
    (data_dir / ".trash/2026-01-01/.discover/b.flac").write_bytes(b"b")
    ops.recover()
    assert statuses(sessions) == ["done", "done"]
    assert files(data_dir / "library") == []


def test_recover_discover_trash_not_moved_rolls_back(
    ops: FileOps, data_dir: Path, journal: SqlJournal, sessions: sessionmaker[Session]
) -> None:
    journal.begin("b", "trash", "b.flac", "2026-01-01/.discover/b.flac", root="discover")
    (data_dir / "discover").mkdir()
    (data_dir / "discover/b.flac").write_bytes(b"b")
    ops.recover()
    assert statuses(sessions) == ["rolled_back"]
    assert (data_dir / "discover/b.flac").read_bytes() == b"b"


@pytest.mark.parametrize("rel", ["../escape.flac", "/abs.flac", ""])
def test_discover_rejects_paths_outside_root(ops: FileOps, make_src: MakeSrc, rel: str) -> None:
    with pytest.raises(SafetyError):
        ops.import_file(make_src(), rel, noop_tags, "b1", root="discover")
    with pytest.raises(SafetyError):
        ops.trash(rel, "b1", root="discover")


@pytest.mark.parametrize("root", ["other", "", "../library", "/tmp"])
def test_unknown_root_refused(ops: FileOps, make_src: MakeSrc, root: str) -> None:
    with pytest.raises(SafetyError):
        ops.import_file(make_src(), REL, noop_tags, "b1", root=root)
    with pytest.raises(SafetyError):
        ops.has(REL, root=root)
    with pytest.raises(SafetyError):
        ops.trash(REL, "b1", root=root)


def test_find_imported_same_src_in_both_roots(ops: FileOps, make_src: MakeSrc) -> None:
    src = make_src()
    ops.import_file(src, REL, noop_tags, "b1")
    ops.import_file(src, "d/x.flac", noop_tags, "b1", root="discover")
    assert ops.find_imported("b1", src) == REL
    assert ops.find_imported("b1", src, root="discover") == "d/x.flac"


def test_discover_trash_restore_round_trip(ops: FileOps, data_dir: Path, make_src: MakeSrc) -> None:
    src = make_src()
    ops.import_file(src, REL, noop_tags, "b1", root="discover")
    t = ops.trash(REL, "b2", root="discover")
    ops.restore(t, REL, "b3", root="discover")
    assert files(data_dir / "discover") == [REL] and files(data_dir / "library") == []
    assert sha(data_dir / "discover" / REL) == sha(src)


def test_restore_refuses_a_root_mismatch(ops: FileOps, data_dir: Path, make_src: MakeSrc) -> None:
    ops.import_file(make_src(), REL, noop_tags, "b1", root="discover")
    ops.import_file(make_src(name="two"), "two.flac", noop_tags, "b1")
    td = ops.trash(REL, "b2", root="discover")
    tl = ops.trash("two.flac", "b2")
    with pytest.raises(SafetyError):
        ops.restore(td, REL, "b3")  # discover trash into the library
    with pytest.raises(SafetyError):
        ops.restore(tl, "two.flac", "b3", root="discover")
    assert files(data_dir / "library") == [] and files(data_dir / "discover") == []


def test_library_path_cannot_use_the_discover_trash_prefix(ops: FileOps, make_src: MakeSrc) -> None:
    ops.import_file(make_src(), ".discover/x.flac", noop_tags, "b1")
    with pytest.raises(SafetyError):
        ops.trash(".discover/x.flac", "b2")


def test_recover_restore_in_discover(
    ops: FileOps, data_dir: Path, journal: SqlJournal, sessions: sessionmaker[Session]
) -> None:
    done = journal.begin("b", "restore", "2026-01-01/.discover/a.flac", "a.flac", root="discover")
    (data_dir / "discover").mkdir()
    (data_dir / "discover/a.flac").write_bytes(b"a")  # back in discover, trash name gone
    todo = journal.begin("b", "restore", "2026-01-01/.discover/b.flac", "b.flac", root="discover")
    (data_dir / ".trash/2026-01-01/.discover").mkdir(parents=True)
    (data_dir / ".trash/2026-01-01/.discover/b.flac").write_bytes(b"b")  # never restored
    ops.recover()
    assert statuses(sessions) == ["done", "rolled_back"] and done < todo
    assert files(data_dir / "library") == []


def test_recover_discover_trash_ignores_same_rel_in_library(
    ops: FileOps, data_dir: Path, journal: SqlJournal, sessions: sessionmaker[Session]
) -> None:
    journal.begin("b", "trash", "b.flac", "2026-01-01/.discover/b.flac", root="discover")
    (data_dir / "library/b.flac").write_bytes(b"lib")
    (data_dir / ".trash/2026-01-01/.discover").mkdir(parents=True)
    (data_dir / ".trash/2026-01-01/.discover/b.flac").write_bytes(b"d")  # moved already
    ops.recover()
    assert statuses(sessions) == ["done"]
    assert (data_dir / "library/b.flac").read_bytes() == b"lib"


def test_only_release_deletes() -> None:
    banned = re.compile(r"\.unlink\(|os\.remove|\brmtree\b|\.rmdir\(|removedirs|os\.unlink")
    src = Path(fileops.__file__).parents[1]
    hits = [p.name for p in src.rglob("*.py") if banned.search(p.read_text())]
    # release() is the one delete of user files; db/backup.py prunes only its own backups
    assert sorted(hits) == ["backup.py", "fileops.py"]


def test_failure_after_publish_stays_pending_and_recovers(
    ops: FileOps,
    data_dir: Path,
    make_src: MakeSrc,
    journal: SqlJournal,
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_step: str) -> None:
        if _step == "rename":
            raise OSError("fsync failed")

    monkeypatch.setattr(fileops, "_hook", boom)
    with pytest.raises(OSError):
        ops.import_file(make_src(), REL, append_tag, "b")
    final = data_dir / "library" / REL
    data = final.read_bytes()
    assert statuses(sessions) == ["started"] and len(journal.pending()) == 1
    ops.recover()
    assert statuses(sessions) == ["done"] and final.read_bytes() == data


def test_clear_work_stays_inside_the_work_dir(ops: FileOps, data_dir: Path, tmp_path: Path) -> None:
    for batch in ("b1", "b2"):
        (ops.work / batch / "0").mkdir(parents=True)
        (ops.work / batch / "0" / "x.flac").write_bytes(b"x")
    outside = tmp_path / "outside"
    outside.mkdir()
    for bad in ("", "..", "../outside", str(outside)):
        with pytest.raises(SafetyError):
            ops.clear_work(bad)
    assert outside.exists() and (ops.work / "b1" / "0" / "x.flac").exists()
    ops.clear_work("b1")
    assert not (ops.work / "b1").exists() and (ops.work / "b2" / "0" / "x.flac").exists()
    ops.recover()  # stale scratch of a crashed run
    assert list(ops.work.iterdir()) == []


def test_recover_clears_work_and_never_follows_a_symlink(ops: FileOps, tmp_path: Path) -> None:
    keep = tmp_path / "outside"
    keep.mkdir()
    (keep / "x").write_bytes(b"x")
    (ops.work / "b1" / "run").mkdir(parents=True)
    (ops.work / "b1" / "run" / "x.flac").write_bytes(b"x")
    (ops.work / "link").symlink_to(keep, target_is_directory=True)
    ops.recover()
    assert not (ops.work / "b1").exists()
    assert (keep / "x").read_bytes() == b"x"


@pytest.mark.parametrize("root", ["library", "discover"])
@pytest.mark.parametrize("link_kind", ["parent", "leaf", "dangling", "root", "internal"])
def test_managed_operations_refuse_symlinks(
    ops: FileOps, tmp_path: Path, make_src: MakeSrc, root: str, link_kind: str
) -> None:
    managed = ops.roots[root]
    outside = tmp_path / "outside"
    outside.mkdir()
    precious = outside / "precious.flac"
    precious.write_bytes(b"precious")
    if link_kind == "root":
        if managed.exists():
            managed.rename(tmp_path / "old-root")
        managed.symlink_to(outside, target_is_directory=True)
        rel = "precious.flac"
    else:
        managed.mkdir(exist_ok=True)
        if link_kind == "leaf":
            (managed / "link").symlink_to(precious)
            rel = "link"
        else:
            target = outside
            if link_kind == "internal":
                target = managed / "real"
                target.mkdir()
                (target / "precious.flac").write_bytes(b"precious")
            elif link_kind == "dangling":
                target = outside / "missing"
            (managed / "link").symlink_to(target, target_is_directory=True)
            rel = "link/precious.flac"
    src = make_src()
    before = src.read_bytes()
    for operation in (
        lambda: ops.import_file(src, rel, noop_tags, "copy", root=root),
        lambda: ops.write_new(rel, b"generated", "cover", root=root),
        lambda: ops.trash(rel, "trash", root=root),
        lambda: ops.has(rel, root=root),
    ):
        with pytest.raises(SafetyError, match="symlink"):
            operation()
    assert precious.read_bytes() == b"precious"
    assert src.read_bytes() == before
    assert sorted(p.name for p in outside.iterdir()) == ["precious.flac"]
    assert not ops.trash_dir.exists() and not ops.incoming.exists()


@pytest.mark.parametrize("side", ["source", "destination"])
def test_restore_refuses_symlinked_paths(ops: FileOps, tmp_path: Path, side: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    precious = outside / "precious.flac"
    precious.write_bytes(b"precious")
    trash_rel, rel = "2026-01-01/precious.flac", "Artist/precious.flac"
    ops.trash_dir.mkdir()
    if side == "source":
        (ops.trash_dir / "2026-01-01").symlink_to(outside, target_is_directory=True)
    else:
        src = ops.trash_dir / trash_rel
        src.parent.mkdir()
        src.write_bytes(b"trashed")
        (ops.library / "Artist").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SafetyError, match="symlink"):
        ops.restore(trash_rel, rel, "restore")
    assert precious.read_bytes() == b"precious"
    assert (ops.trash_dir / trash_rel).read_bytes() == (
        b"precious" if side == "source" else b"trashed"
    )
    assert sorted(p.name for p in outside.iterdir()) == ["precious.flac"]


def test_trash_refuses_symlinked_destination(ops: FileOps, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (ops.library / "precious.flac").write_bytes(b"precious")
    ops.trash_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SafetyError, match="symlink"):
        ops.trash("precious.flac", "trash")
    assert (ops.library / "precious.flac").read_bytes() == b"precious"
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("kind", ["copy", "trash", "restore"])
def test_recovery_refuses_symlinked_journal_paths(
    ops: FileOps, tmp_path: Path, journal: SqlJournal, kind: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    precious = outside / "precious.flac"
    precious.write_bytes(b"precious")
    (ops.library / "Artist").symlink_to(outside, target_is_directory=True)
    rel, trash_rel = "Artist/precious.flac", "2026-01-01/Artist/precious.flac"
    src, dst = (rel, trash_rel) if kind == "trash" else (trash_rel, rel)
    op = journal.begin("b", kind, src, dst)
    journal.set_dst_hash(op, sha(precious))
    with pytest.raises(SafetyError, match="symlink"):
        ops.recover()
    assert precious.read_bytes() == b"precious"
    assert [pending.id for pending in journal.pending()] == [op]
    assert not ops.trash_dir.exists()


def test_release_refuses_a_verified_import_redirected_by_symlink(
    ops: FileOps, tmp_path: Path, make_src: MakeSrc
) -> None:
    src = make_src()
    ops.import_file(src, "Artist/song.flac", noop_tags, "b")
    original = tmp_path / "original"
    (ops.library / "Artist").rename(original)
    (ops.library / "Artist").symlink_to(original, target_is_directory=True)
    with pytest.raises(SafetyError, match="symlink"):
        ops.release(src, "b")
    assert src.read_bytes() == (original / "song.flac").read_bytes()


def test_data_directory_can_have_a_symlinked_ancestor(
    ops: FileOps, data_dir: Path, tmp_path: Path, journal: SqlJournal, make_src: MakeSrc
) -> None:
    alias = tmp_path / "data-alias"
    alias.symlink_to(data_dir, target_is_directory=True)
    linked = FileOps(alias, tmp_path / "downloads", journal, SystemClock())
    src = make_src()
    linked.import_file(src, REL, noop_tags, "b")
    trashed = linked.trash(REL, "b")
    linked.restore(trashed, REL, "b")
    assert (data_dir / "library" / REL).read_bytes() == src.read_bytes()
    assert linked.has(REL)


@pytest.mark.parametrize("operation", ["import", "write"])
def test_staging_refuses_a_symlinked_root(
    ops: FileOps, tmp_path: Path, make_src: MakeSrc, operation: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    ops.incoming.symlink_to(outside, target_is_directory=True)
    src = make_src()
    before = src.read_bytes()
    with pytest.raises(SafetyError, match="symlink"):
        if operation == "import":
            ops.import_file(src, REL, noop_tags, "b")
        else:
            ops.write_new(REL, b"cover", "b")
    assert src.read_bytes() == before
    assert list(outside.iterdir()) == []
    assert files(ops.library) == []


@pytest.mark.parametrize("link_kind", ["parent", "leaf"])
def test_recovery_skips_a_symlinked_pending_purge(
    ops: FileOps,
    tmp_path: Path,
    journal: SqlJournal,
    sessions: sessionmaker[Session],
    link_kind: str,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    precious = outside / "precious.flac"
    precious.write_bytes(b"precious")
    day = ops.trash_dir / "2026-01-01"
    day.mkdir(parents=True)
    if link_kind == "parent":
        (day / "link").symlink_to(outside, target_is_directory=True)
        rel = "2026-01-01/link/precious.flac"
    else:
        (day / "link").symlink_to(precious)
        rel = "2026-01-01/link"
    journal.begin("b", "purge", rel, "")
    ops.recover()
    assert precious.read_bytes() == b"precious"
    assert statuses(sessions) == ["rolled_back"]
    assert journal.pending() == []
