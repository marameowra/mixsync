from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import SystemClock
from mixsync.core.config import Settings
from mixsync.core.matching import AlbumInfo, TrackInfo
from mixsync.db.models.library import Track
from mixsync.db.models.safety import TagSnapshot
from mixsync.library import fileops, tags
from mixsync.library.fileops import FileOps
from mixsync.library.importer import Importer

from .conftest import MakeSrc

ARTIST = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
ALBUM = AlbumInfo(
    "Radiohead", "Kid A", (), year=2000, release_id="rel", release_group_id="rg",
    artist_ids=(ARTIST,),
)  # fmt: skip
T1 = TrackInfo("Everything in Its Right Place", "Radiohead", medium=1, medium_index=1,
               recording_id="rec1", artist_ids=(ARTIST,))  # fmt: skip
T2 = TrackInfo("Kid A", medium=1, medium_index=2, recording_id="rec2")


@pytest.fixture
def importer(ops: FileOps, sessions: sessionmaker[Session], settings: Settings) -> Importer:
    return Importer(ops, sessions, SystemClock(), settings.path_template)


def test_import_release(
    importer: Importer, ops: FileOps, make_src: MakeSrc, sessions: sessionmaker[Session]
) -> None:
    a, b = make_src("flac", "a"), make_src("mp3", "b")
    results = importer.import_release([(a, T1, "ac-1"), (b, T2, None)], ALBUM, "verified", "batch")
    assert [r.rel for r in results] == [
        "Radiohead/Kid A (2000)/01-01 Everything in Its Right Place.flac",
        "Radiohead/Kid A (2000)/01-02 Kid A.mp3",
    ]
    assert a.exists() and b.exists()  # sources are the caller's to release
    got = tags.read_all(ops.library / str(results[0].rel))["tags"]
    assert got["musicbrainz_trackid"] == ["rec1"] and got["acoustid_id"] == ["ac-1"]
    assert got["mixsync_status"] == ["verified"] and got["musicbrainz_releasegroupid"] == ["rg"]
    with sessions() as s:
        rows = s.scalars(select(Track).order_by(Track.id)).all()
        assert [(t.path, t.recording_mbid, t.status) for t in rows] == [
            (results[0].rel, "rec1", "verified"),
            (results[1].rel, "rec2", "verified"),
        ]
        assert rows[0].artist_mbids == [ARTIST] and rows[0].acoustid_id == "ac-1"
        snap = s.scalars(select(TagSnapshot)).first()
        assert snap and snap.tags["format"] == "flac" and snap.file_path == results[0].rel


def test_failing_file_leaves_no_partial_and_keeps_the_others(
    importer: Importer,
    ops: FileOps,
    make_src: MakeSrc,
    tmp_path: Path,
    sessions: sessionmaker[Session],
) -> None:
    bad = tmp_path / "downloads" / "bad.flac"
    bad.parent.mkdir(exist_ok=True)
    bad.write_bytes(b"not audio")
    good = make_src("flac", "good")
    r_bad, r_good = importer.import_release([(bad, T1, None), (good, T2, None)], ALBUM, "x", "b")
    assert r_bad.rel is None and r_bad.error and r_good.rel and r_good.error is None
    assert [p.name for p in ops.library.rglob("*.flac")] == ["01-02 Kid A.flac"]
    assert bad.read_bytes() == b"not audio"
    with sessions() as s:
        assert [t.path for t in s.scalars(select(Track))] == [r_good.rel]


def test_name_collision_gets_suffix(importer: Importer, make_src: MakeSrc) -> None:
    r1 = importer.import_release([(make_src("flac", "a"), T1, None)], ALBUM, "x", "b1")
    r2 = importer.import_release([(make_src("flac", "b"), T1, None)], ALBUM, "x", "b2")
    assert r2[0].rel == str(r1[0].rel).replace(".flac", " (2).flac")


def test_exdev_in_importer_is_reported_per_file(
    importer: Importer, make_src: MakeSrc, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_s: Path, _d: Path) -> bool:
        raise OSError(18, "cross-device")

    monkeypatch.setattr(fileops, "_noreplace", boom)
    (r,) = importer.import_release([(make_src(), T1, None)], ALBUM, "x", "b")
    assert r.rel is None and "SafetyError" in str(r.error)
