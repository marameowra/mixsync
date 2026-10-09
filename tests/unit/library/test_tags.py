import json
from pathlib import Path

import pytest

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
