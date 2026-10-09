import json
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import TDRC, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Tags
from mutagen.oggvorbis import OggVorbis

from mixsync.library import tags

from .conftest import FORMATS, MakeSrc

RECORDING = "60bd9d53-01ff-4562-8058-eb44b3940317"
RELEASE = "18e2b05b-937a-3c25-ac78-07267e6411a0"
GROUP = "e75c0549-ad55-39e3-8025-c72c5d4a3c5d"
ARTIST = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
TAGSET = tags.TagSet(
    "Idioteque", "Radiohead", "Kid A", "Radiohead", 8, 1, 2000, RECORDING, RELEASE, GROUP,
    (ARTIST,), (ARTIST,), "acoustid-1", "verified",
)  # fmt: skip
OTHER = tags.TagSet("Old", "Old A", "Old Album", "Old AA", 1, 2, 1999)

_VORBIS = (
    "musicbrainz_trackid musicbrainz_albumid musicbrainz_releasegroupid musicbrainz_artistid "
    "musicbrainz_albumartistid acoustid_id mixsync_status"
).split()
_MP4 = "----:com.apple.iTunes:"
# Picard's field names, per format, in the order of VALUES
NATIVE = {
    "flac": _VORBIS,
    "ogg": _VORBIS,
    "mp3": [
        "UFID:http://musicbrainz.org", "TXXX:MusicBrainz Album Id",
        "TXXX:MusicBrainz Release Group Id", "TXXX:MusicBrainz Artist Id",
        "TXXX:MusicBrainz Album Artist Id", "TXXX:Acoustid Id", "TXXX:MIXSYNC_STATUS",
    ],
    "m4a": [
        _MP4 + n
        for n in (
            "MusicBrainz Track Id", "MusicBrainz Album Id", "MusicBrainz Release Group Id",
            "MusicBrainz Artist Id", "MusicBrainz Album Artist Id", "Acoustid Id",
            "MIXSYNC_STATUS",
        )
    ],
}  # fmt: skip
VALUES = [RECORDING, RELEASE, GROUP, ARTIST, ARTIST, "acoustid-1", "verified"]
TITLE = {"flac": "title", "ogg": "title", "mp3": "TIT2", "m4a": "\xa9nam"}
TRACK = {"flac": "tracknumber", "ogg": "tracknumber", "mp3": "TRCK", "m4a": "trkn"}


@pytest.mark.parametrize("fmt", FORMATS)
def test_write_uses_picard_names(fmt: str, make_src: MakeSrc) -> None:
    f = make_src(fmt)
    tags.write(f, TAGSET)
    got = tags.read_all(f)["tags"]
    for key, value in zip(NATIVE[fmt], VALUES, strict=True):
        assert got[key] == [value], key
    assert got[TITLE[fmt]] == ["Idioteque"]
    assert got[TRACK[fmt]] == ["8"]


@pytest.mark.parametrize("fmt", FORMATS)
def test_snapshot_and_restore(fmt: str, make_src: MakeSrc) -> None:
    f = make_src(fmt)
    blank = tags.read_all(f)
    tags.write(f, OTHER)
    before = tags.read_all(f)
    tags.write(f, TAGSET)
    assert tags.read_all(f) != before
    tags.restore(f, before)
    assert tags.read_all(f) == before
    tags.restore(f, blank)
    assert tags.read_all(f) == blank


def test_snapshot_is_json(make_src: MakeSrc) -> None:
    f = make_src("m4a")
    tags.write(f, TAGSET)
    assert json.loads(json.dumps(tags.read_all(f))) == tags.read_all(f)


def test_unsupported_file(tmp_path: Path) -> None:
    bad = tmp_path / "x.txt"
    bad.write_text("nope")
    with pytest.raises(ValueError):
        tags.read_all(bad)


WRONG_ID = "00000000-0000-0000-0000-000000000000"
GONE = {  # the wrong tags planted by _pretag_wrong, by native key
    "flac": ["date", "musicbrainz_releasegroupid", "musicbrainz_artistid", "acoustid_id"],
    "ogg": ["date", "musicbrainz_releasegroupid", "musicbrainz_artistid", "acoustid_id"],
    "mp3": ["TDRC", "TXXX:MusicBrainz Release Group Id", "TXXX:MusicBrainz Artist Id",
            "TXXX:Acoustid Id"],
    "m4a": ["\xa9day", _MP4 + "MusicBrainz Release Group Id", _MP4 + "MusicBrainz Artist Id",
            _MP4 + "Acoustid Id"],
}  # fmt: skip


def _pretag_wrong(fmt: str, path: Path) -> None:
    """Wrong date, release group id, artist id and acoustid id, as found in downloads."""
    values = ["1066", WRONG_ID, WRONG_ID, "wrong"]
    if fmt in ("flac", "ogg"):
        f = FLAC(path) if fmt == "flac" else OggVorbis(path)
        assert f.tags is not None
        for k, v in zip(  # UPPERCASE: deletion has to be case-insensitive
            ["DATE", "MUSICBRAINZ_RELEASEGROUPID", "MUSICBRAINZ_ARTISTID", "ACOUSTID_ID"],
            values,
            strict=True,
        ):
            f.tags[k] = [v]
    elif fmt == "mp3":
        f = MP3(path)
        f.add_tags()
        assert f.tags is not None
        f.tags.add(TDRC(encoding=3, text=[values[0]]))
        for k, v in zip(GONE[fmt][1:], values[1:], strict=True):
            f.tags.add(TXXX(encoding=3, desc=k[5:], text=[v]))
    else:
        f = MP4(path)
        f.tags = MP4Tags()
        for k, v in zip(GONE[fmt], values, strict=True):
            f.tags[k] = [v] if k == "\xa9day" else [v.encode()]
    f.save()


@pytest.mark.parametrize("fmt", FORMATS)
def test_sk_q08_wrong_source_tags_do_not_survive_import(fmt: str, make_src: MakeSrc) -> None:
    f = make_src(fmt)
    _pretag_wrong(fmt, f)
    original = tags.read_all(f)
    assert all(k in original["tags"] for k in GONE[fmt])
    tags.write(f, tags.TagSet("T", "A", "Al", "AA", 1, 1, None, RECORDING, RELEASE))
    got = tags.read_all(f)["tags"]
    assert [k for k in GONE[fmt] if k in got] == []
    assert got[NATIVE[fmt][1]] == [RELEASE] and got[TITLE[fmt]] == ["T"]
    tags.restore(f, original)
    assert tags.read_all(f) == original
