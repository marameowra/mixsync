from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import SystemClock
from mixsync.core.config import Settings
from mixsync.core.matching import AlbumInfo, TrackInfo
from mixsync.db.models.discovery import ProvisionalTrack
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


def test_resume_after_crash_window_makes_no_duplicates(
    importer: Importer, ops: FileOps, make_src: MakeSrc, sessions: sessionmaker[Session]
) -> None:
    files = [(make_src("flac", "a"), T1, "ac-1"), (make_src("mp3", "b"), T2, None)]
    first = importer.import_release(files, ALBUM, "verified", "batch")
    with sessions.begin() as s:
        for t in s.scalars(select(Track)):
            s.delete(t)  # the process died before the rows were written
    again = importer.import_release(files, ALBUM, "verified", "batch")
    assert [r.rel for r in again] == [r.rel for r in first]
    assert len(list(ops.library.rglob("*.*"))) == 2  # no " (2)" copies
    with sessions() as s:
        assert sorted(t.path for t in s.scalars(select(Track))) == sorted(str(r.rel) for r in first)
    importer.import_release(files, ALBUM, "verified", "batch")  # idempotent on rows
    with sessions() as s:
        assert len(s.scalars(select(Track)).all()) == 2


def test_discover_import_writes_no_track_row_checks_discover_names_and_resumes(
    importer: Importer, ops: FileOps, make_src: MakeSrc, sessions: sessionmaker[Session]
) -> None:
    lib = importer.import_release([(make_src("flac", "a"), T1, None)], ALBUM, "x", "b0")
    src = make_src("flac", "b")
    (r,) = importer.import_release([(src, T1, None)], ALBUM, "verified", "b1", root="discover")
    assert r.rel == lib[0].rel  # a library file of that name does not make discover rename
    assert (ops.roots["discover"] / str(r.rel)).is_file()
    assert (
        ops.find_imported("b1", src, "discover") is not None
        and ops.find_imported("b1", src) is None
    )
    (again,) = importer.import_release([(src, T1, None)], ALBUM, "verified", "b1", root="discover")
    assert again.rel == r.rel and len(list(ops.roots["discover"].rglob("*.flac"))) == 1
    (r2,) = importer.import_release(
        [(make_src("flac", "c"), T1, None)], ALBUM, "x", "b2", root="discover"
    )
    assert r2.rel == str(r.rel).replace(".flac", " (2).flac")
    with sessions() as s:
        assert [t.path for t in s.scalars(select(Track))] == [lib[0].rel]


def test_discover_path_held_by_an_active_row_is_taken_even_without_its_file(
    importer: Importer, make_src: MakeSrc, sessions: sessionmaker[Session]
) -> None:
    rel = "Radiohead/Kid A (2000)/01-01 Everything in Its Right Place.flac"
    with sessions.begin() as s:  # the file was deleted by hand; the row remains
        s.add(
            ProvisionalTrack(
                path=rel, recording_mbid="old", batch_id="b0", imported_at=SystemClock().now()
            )
        )
    (r,) = importer.import_release(
        [(make_src("flac", "a"), T1, None)], ALBUM, "verified", "b1", root="discover"
    )
    assert r.error is None and r.rel == rel.replace(".flac", " (2).flac")


def test_retrying_a_batch_after_its_file_was_deleted_reuses_path_and_row(
    importer: Importer, ops: FileOps, make_src: MakeSrc, sessions: sessionmaker[Session]
) -> None:
    src = make_src("flac", "a")
    (first,) = importer.import_release([(src, T1, None)], ALBUM, "x", "b1")
    assert first.rel
    (ops.library / first.rel).unlink()
    (again,) = importer.import_release([(src, T1, None)], ALBUM, "x", "b1")
    assert again.rel == first.rel and (ops.library / first.rel).is_file()
    with sessions() as s:
        assert [t.path for t in s.scalars(select(Track))] == [first.rel]


def test_a_row_whose_file_is_missing_still_holds_its_library_path(
    importer: Importer, ops: FileOps, make_src: MakeSrc, sessions: sessionmaker[Session]
) -> None:
    (first,) = importer.import_release([(make_src("flac", "a"), T1, None)], ALBUM, "x", "b1")
    assert first.rel
    (ops.library / first.rel).unlink()  # the row outlives the file
    t = TrackInfo(T1.title, "Radiohead", medium=1, medium_index=1, recording_id="rec-new")
    (second,) = importer.import_release([(make_src("flac", "b"), t, None)], ALBUM, "x", "b2")
    assert second.rel == first.rel.replace(".flac", " (2).flac")
    with sessions() as s:
        rows = {r.path: r.recording_mbid for r in s.scalars(select(Track))}
    assert rows == {first.rel: "rec1", second.rel: "rec-new"}
