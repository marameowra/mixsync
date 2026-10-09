import errno
import hashlib
import re
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

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


def test_nothing_in_src_deletes() -> None:
    banned = re.compile(r"\.unlink\(|os\.remove|\brmtree\b|\.rmdir\(|removedirs|os\.unlink")
    src = Path(fileops.__file__).parents[1]
    hits = [str(p) for p in src.rglob("*.py") if banned.search(p.read_text())]
    assert hits == []


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
