from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import SystemClock
from mixsync.core.errors import SafetyError
from mixsync.db.journal import SqlJournal
from mixsync.library import fileops
from mixsync.library.fileops import FileOps

from .conftest import MakeSrc
from .test_fileops import REL, append_tag, statuses


def imported(ops: FileOps, make_src: MakeSrc, name: str = "src") -> Path:
    src = make_src(name=name)
    ops.import_file(src, REL.replace("Song", name), append_tag, "b1")
    return src


def test_release_deletes_the_file_and_the_empty_folder(
    ops: FileOps, make_src: MakeSrc, sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    src = imported(ops, make_src)
    album = tmp_path / "downloads" / "Album"
    album.mkdir()
    moved = src.rename(album / src.name)
    ops.import_file(moved, "A/B/x.flac", append_tag, "b2")
    ops.release(moved, "b2")
    assert not moved.exists() and not album.exists() and src.parent.exists()
    assert statuses(sessions) == ["done", "done", "done"]
    ops.release(moved, "b2")  # a retry with the file already gone still succeeds


def test_release_refuses_outside_downloads(ops: FileOps, tmp_path: Path) -> None:
    out = tmp_path / "elsewhere.flac"
    out.write_bytes(b"x")
    with pytest.raises(SafetyError):
        ops.release(out, "b1")
    with pytest.raises(SafetyError):
        ops.release(tmp_path / "downloads" / ".." / "elsewhere.flac", "b1")
    assert out.exists()


def test_release_refuses_a_symlink_escape(ops: FileOps, make_src: MakeSrc, tmp_path: Path) -> None:
    src = imported(ops, make_src)
    link = src.with_name("link.flac")
    link.symlink_to(src)
    with pytest.raises(SafetyError):
        ops.release(link, "b1")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "f.flac").write_bytes(b"x")
    (tmp_path / "downloads" / "dirlink").symlink_to(tmp_path / "out")
    with pytest.raises(SafetyError):
        ops.release(tmp_path / "downloads" / "dirlink" / "f.flac", "b1")
    assert src.exists() and (tmp_path / "out" / "f.flac").exists()


def test_release_needs_a_verified_import_in_this_batch(ops: FileOps, make_src: MakeSrc) -> None:
    src = imported(ops, make_src)
    with pytest.raises(SafetyError):
        ops.release(src, "other-batch")
    (ops.library / REL.replace("Song", "src")).write_bytes(b"corrupt")  # copy no longer verifies
    with pytest.raises(SafetyError):
        ops.release(src, "b1")
    assert src.exists()


def test_crash_between_begin_and_unlink_recovers_and_retries(
    ops: FileOps,
    make_src: MakeSrc,
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = imported(ops, make_src)

    def crash(step: str) -> None:
        if step == "release":
            raise KeyboardInterrupt

    monkeypatch.setattr(fileops, "_hook", crash)
    with pytest.raises(KeyboardInterrupt):
        ops.release(src, "b1")
    monkeypatch.undo()
    assert src.exists() and statuses(sessions) == ["done", "started"]
    ops.recover()
    assert statuses(sessions) == ["done", "rolled_back"]
    ops.release(src, "b1")
    assert not src.exists() and statuses(sessions) == ["done", "rolled_back", "done"]


def test_release_after_discover_import(ops: FileOps, make_src: MakeSrc, data_dir: Path) -> None:
    src = make_src()
    ops.import_file(src, REL, append_tag, "b1", root="discover")
    with pytest.raises(SafetyError):  # imported into discover, not the library
        ops.release(src, "b1")
    assert src.exists()
    ops.release(src, "b1", root="discover")
    assert not src.exists() and (data_dir / "discover" / REL).exists()


def test_a_usenet_download_is_released_with_the_same_checks_as_downloads(
    data_dir: Path, journal: SqlJournal, make_src: MakeSrc, tmp_path: Path
) -> None:
    usenet = tmp_path / "usenet"
    ops = FileOps(data_dir, tmp_path / "downloads", journal, SystemClock(), usenet_dir=usenet)
    (usenet / "Job").mkdir(parents=True)
    src = make_src().rename(usenet / "Job" / "src.flac")
    (usenet / "link.flac").symlink_to(src)
    ops.import_file(src, "A/B/x.flac", append_tag, "b1")
    with pytest.raises(SafetyError):
        ops.release(usenet / "link.flac", "b1")
    with pytest.raises(SafetyError):
        ops.release(usenet / "Job" / ".." / "Job" / "src.flac", "b1")
    ops.release(src, "b1")
    assert not src.exists() and not (usenet / "Job").exists() and usenet.is_dir()
    other = make_src(name="other").rename(usenet / "other.flac")
    copy = ops.quarantine(other, "rejected", "q1")  # a verified copy, then a release
    assert copy.is_file() and not other.exists() and not ops.seeding(copy)


def test_release_and_quarantine_never_touch_a_seeding_download(
    data_dir: Path, journal: SqlJournal, make_src: MakeSrc, tmp_path: Path
) -> None:
    """ADR 0022 backstop: whatever the caller asks, a file under the qBittorrent save path is
    copied, never released, moved or deleted."""
    seeding = tmp_path / "torrents"
    ops = FileOps(data_dir, tmp_path / "downloads", journal, SystemClock(), seeding_dir=seeding)
    (seeding / "Album").mkdir(parents=True)
    src = make_src().rename(seeding / "Album" / "src.flac")
    before = src.read_bytes()
    ops.import_file(src, "A/B/x.flac", append_tag, "b1")
    with pytest.raises(SafetyError, match="seeding download"):
        ops.release(src, "b1")
    copy = ops.quarantine(src, "rejected", "q1")
    assert copy.read_bytes() == before and copy.parent.name == "Album"
    assert ops.quarantine(src, "review", "q2").read_bytes() == before
    assert src.read_bytes() == before and ops.seeding(src)
    assert not ops.seeding(tmp_path / "downloads" / "x.flac")
