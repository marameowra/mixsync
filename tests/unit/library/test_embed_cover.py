import shutil
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import APIC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import SystemClock
from mixsync.core.matching import AlbumInfo, TrackInfo
from mixsync.library import tag_undo, tags
from mixsync.library.fileops import FileOps
from mixsync.library.importer import Importer, tagset_for

from .conftest import MakeSrc

ALBUM = AlbumInfo("Radiohead", "Kid A", (), year=2000, release_id="rel", release_group_id="rg")
TRACK = TrackInfo("Kid A", medium=1, medium_index=2, recording_id="rec2")
IMG = b"\xff\xd8\xff\xe0fake-jpeg"


def front(path: Path) -> bytes | None:
    """The type-3 picture bytes, read with each format's own library."""
    if path.suffix == ".flac":
        return next((p.data for p in FLAC(path).pictures if p.type == 3), None)
    if path.suffix == ".mp3":
        return next((f.data for f in MP3(path).tags.getall("APIC") if f.type == 3), None)  # pyright: ignore[reportOptionalMemberAccess]
    covr = (MP4(path).tags or {}).get("covr")
    return bytes(covr[0]) if covr else None


def do_import(
    ops: FileOps, sessions: sessionmaker[Session], src: Path, cover: bytes | None
) -> Path:
    importer = Importer(ops, sessions, SystemClock(), "{title}.{ext}")
    (result,) = importer.import_release([(src, TRACK, None)], ALBUM, "verified", "b", cover=cover)
    assert result.rel
    return ops.library / result.rel


@pytest.mark.parametrize("fmt", ["flac", "mp3", "m4a"])
def test_cover_is_embedded_and_restore_removes_it(
    fmt: str, ops: FileOps, sessions: sessionmaker[Session], make_src: MakeSrc
) -> None:
    src = make_src(fmt)
    old = tags.read_all(src)
    out = do_import(ops, sessions, src, IMG)
    assert front(out) == IMG
    tag_undo.restore_tags(ops, sessions, SystemClock(), 1, out.name, old)
    assert front(out) is None
    assert tags.read_all(out) == old


@pytest.mark.parametrize("fmt", ["flac", "mp3", "m4a"])
def test_without_a_cover_the_import_is_just_the_tag_write(
    fmt: str, ops: FileOps, sessions: sessionmaker[Session], make_src: MakeSrc
) -> None:
    src = make_src(fmt)
    plain = src.with_name(f"plain.{fmt}")
    shutil.copy(src, plain)
    tags.write(plain, tagset_for(TRACK, ALBUM, None, "verified"))
    assert do_import(ops, sessions, src, None).read_bytes() == plain.read_bytes()


def test_existing_art_is_left_alone(
    ops: FileOps, sessions: sessionmaker[Session], make_src: MakeSrc
) -> None:
    src = make_src("mp3")
    f = MP3(src)
    f.add_tags()
    f.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=b"mine"))  # pyright: ignore[reportOptionalMemberAccess]
    f.save()
    old = tags.read_all(src)
    out = do_import(ops, sessions, src, IMG)
    assert front(out) == b"mine"
    tag_undo.restore_tags(ops, sessions, SystemClock(), 1, out.name, old)
    assert front(out) == b"mine"  # the snapshot had art, so a restore keeps it


@pytest.mark.parametrize("fmt", ["flac", "mp3", "m4a"])
def test_snapshot_from_before_cover_tracking_never_strips_art(
    fmt: str, ops: FileOps, sessions: sessionmaker[Session], make_src: MakeSrc
) -> None:
    src = make_src(fmt)
    old = tags.read_all(src)
    del old["cover"]  # snapshots stored before this feature have no cover key
    out = do_import(ops, sessions, src, IMG)
    tag_undo.restore_tags(ops, sessions, SystemClock(), 1, out.name, old)
    assert front(out) == IMG
