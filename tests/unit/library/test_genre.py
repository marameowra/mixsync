from dataclasses import replace

import pytest
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import SystemClock
from mixsync.core.matching import AlbumInfo, TrackInfo
from mixsync.library import tags
from mixsync.library.fileops import FileOps
from mixsync.library.importer import Importer
from mixsync.library.paths import render

from .conftest import FORMATS, MakeSrc
from .test_tags import TAGSET

GENRE = {"flac": "genre", "ogg": "genre", "mp3": "TCON", "m4a": "\xa9gen"}
TRACK = TrackInfo("Kid A", medium=1, medium_index=2, recording_id="rec2")


@pytest.mark.parametrize("fmt", FORMATS)
def test_genre_top_three_then_cleared_and_restored(fmt: str, make_src: MakeSrc) -> None:
    f = make_src(fmt)
    blank = tags.read_all(f)
    tags.write(f, replace(TAGSET, genres=("rock", "art rock", "electronic", "idm")))
    snap = tags.read_all(f)
    assert snap["tags"][GENRE[fmt]] == ["rock", "art rock", "electronic"]
    tags.write(f, TAGSET)  # unset genres clear the file's own GENRE
    assert GENRE[fmt] not in tags.read_all(f)["tags"]
    tags.restore(f, snap)
    assert tags.read_all(f) == snap
    tags.restore(f, blank)
    assert tags.read_all(f) == blank


def test_genre_path_field() -> None:
    args = dict(albumartist="A", album="B", year=2000, disc=1, track=2, title="T", ext="flac")
    template = "{genre}/{title}.{ext}"
    assert render(template, **args, genre="art/rock") == "artrock/T.flac"  # pyright: ignore[reportArgumentType]
    assert render(template, **args) == "Unknown Genre/T.flac"  # pyright: ignore[reportArgumentType]


def test_import_uses_top_genre_for_path_and_top_three_for_tag(
    ops: FileOps, sessions: sessionmaker[Session], make_src: MakeSrc
) -> None:
    album = AlbumInfo("A", "B", (), genres=("rock", "idm", "ambient", "pop"))
    importer = Importer(ops, sessions, SystemClock(), "{genre}/{album}/{title}.{ext}")
    (res,) = importer.import_release([(make_src(), TRACK, None)], album, "verified", "g")
    assert res.rel == "rock/B/Kid A.flac"
    got = tags.read_all(ops.library / "rock/B/Kid A.flac")["tags"]
    assert got["genre"] == ["rock", "idm", "ambient"]
