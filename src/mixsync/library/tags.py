"""Tag read/write with Picard's field names. FLAC, MP3 (ID3), M4A, Ogg Vorbis.

A snapshot is `{"format": ..., "tags": {native key: [str, ...]}}`. `write` only touches the
managed keys, so `restore` only has to put those back; unmanaged frames (cover art...) are
never modified. The snapshot lists binary frames/atoms as absent.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import mutagen
import mutagen.id3
from mutagen.flac import FLAC
from mutagen.id3 import TXXX, UFID
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.oggvorbis import OggVorbis

Snapshot = dict[str, Any]
_ITUNES = "----:com.apple.iTunes:"
_MB_UFID = "UFID:http://musicbrainz.org"


@dataclass(frozen=True)
class TagSet:
    title: str
    artist: str
    album: str
    albumartist: str
    track: int
    disc: int
    year: int | None = None
    recording_id: str | None = None
    release_id: str | None = None
    release_group_id: str | None = None
    artist_ids: tuple[str, ...] = ()
    albumartist_ids: tuple[str, ...] = ()
    acoustid_id: str | None = None
    status: str = "unverified"  # MIXSYNC_STATUS: verified | unverified


def _fields(t: TagSet) -> dict[str, list[str]]:
    """Picard's Vorbis names; the other formats map from these."""
    f: dict[str, list[str | None]] = {
        "title": [t.title],
        "artist": [t.artist],
        "album": [t.album],
        "albumartist": [t.albumartist],
        "tracknumber": [str(t.track)],
        "discnumber": [str(t.disc)],
        "date": [None if t.year is None else str(t.year)],
        "musicbrainz_trackid": [t.recording_id],
        "musicbrainz_albumid": [t.release_id],
        "musicbrainz_releasegroupid": [t.release_group_id],
        "musicbrainz_artistid": list(t.artist_ids),
        "musicbrainz_albumartistid": list(t.albumartist_ids),
        "acoustid_id": [t.acoustid_id],
        "mixsync_status": [t.status],
    }
    return {k: [x for x in v if x] for k, v in f.items() if any(v)}


_ALL: Sequence[str] = (
    "title artist album albumartist tracknumber discnumber date musicbrainz_trackid "
    "musicbrainz_albumid musicbrainz_releasegroupid musicbrainz_artistid "
    "musicbrainz_albumartistid acoustid_id mixsync_status"
).split()
_ID3: dict[str, str] = dict(
    zip(
        _ALL,
        [
            "TIT2", "TPE1", "TALB", "TPE2", "TRCK", "TPOS", "TDRC", _MB_UFID,
            "TXXX:MusicBrainz Album Id", "TXXX:MusicBrainz Release Group Id",
            "TXXX:MusicBrainz Artist Id", "TXXX:MusicBrainz Album Artist Id",
            "TXXX:Acoustid Id", "TXXX:MIXSYNC_STATUS",
        ],
        strict=True,
    )
)  # fmt: skip
_MP4: dict[str, str] = dict(
    zip(
        _ALL,
        [
            "\xa9nam", "\xa9ART", "\xa9alb", "aART", "trkn", "disk", "\xa9day",
            *(
                _ITUNES + n
                for n in (
                    "MusicBrainz Track Id", "MusicBrainz Album Id",
                    "MusicBrainz Release Group Id", "MusicBrainz Artist Id",
                    "MusicBrainz Album Artist Id", "Acoustid Id", "MIXSYNC_STATUS",
                )
            ),
        ],
        strict=True,
    )
)  # fmt: skip


def _sniff(path: Path) -> Any:
    return cast(Any, mutagen.File(path))  # pyright: ignore[reportUnknownMemberType]  # by content


def _kind(f: object) -> str | None:
    if isinstance(f, FLAC):
        return "flac"
    if isinstance(f, OggVorbis):
        return "ogg"
    if isinstance(f, MP3):
        return "mp3"
    return "m4a" if isinstance(f, MP4) else None


def _open(path: Path) -> tuple[str, Any]:
    f = _sniff(path)
    fmt = _kind(f)
    if fmt is None:
        raise ValueError(f"unsupported audio file: {path}")
    if fmt == "mp3" and f.tags is None:
        f.add_tags()
    return fmt, f


def _text(v: Any) -> str | None:
    if isinstance(v, MP4FreeForm | bytes):
        return bytes(v).decode(errors="replace")
    if isinstance(v, tuple):  # trkn / disk: (n, total)
        n, total = v  # pyright: ignore[reportUnknownVariableType]
        return f"{n}/{total}" if total else str(n)  # pyright: ignore[reportUnknownArgumentType]
    return v if isinstance(v, str) else None


def read_all(path: Path) -> Snapshot:
    fmt, f = _open(path)
    tags: dict[str, list[str]] = {}
    if fmt == "mp3":
        for key, frame in f.tags.items():
            if isinstance(frame, UFID):
                tags[key] = [bytes(frame.data).decode(errors="replace")]  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportAttributeAccessIssue]
            elif hasattr(frame, "text"):
                tags[key] = [str(t) for t in frame.text]
    else:
        for key, values in cast(dict[str, Any], f.tags or {}).items():
            vals = [t for v in values if (t := _text(v)) is not None]
            if vals:
                tags[key.lower() if fmt != "m4a" else key] = vals
    return {"format": fmt, "tags": tags}


def _set(fmt: str, f: Any, native: str, values: list[str]) -> None:
    if fmt in ("flac", "ogg"):
        f.tags[native] = values
    elif fmt == "m4a":
        if native in ("trkn", "disk"):
            n, _, total = values[0].partition("/")
            f.tags[native] = [(int(n), int(total or 0))]
        elif native.startswith("----"):
            f.tags[native] = [v.encode() for v in values]
        else:
            f.tags[native] = values
    elif native.startswith("UFID:"):
        f.tags.setall(native, [UFID(owner=native[5:], data=values[0].encode())])
    elif native.startswith("TXXX:"):
        f.tags.setall(native, [TXXX(encoding=3, desc=native[5:], text=values)])
    else:
        f.tags.setall(native, [getattr(mutagen.id3, native)(encoding=3, text=values)])


def _del(fmt: str, f: Any, native: str) -> None:
    if fmt == "mp3":
        f.tags.delall(native)
    elif native in (f.tags or {}):
        del f.tags[native]


def _natives(fmt: str) -> dict[str, str]:
    return _ID3 if fmt == "mp3" else _MP4 if fmt == "m4a" else {k: k for k in _ALL}


def write(path: Path, tags: TagSet) -> None:
    fmt, f = _open(path)
    natives = _natives(fmt)
    for name, values in _fields(tags).items():
        _set(fmt, f, natives[name], values)
    f.save()


def restore(path: Path, snapshot: Snapshot) -> None:
    """Undo a `write`: managed keys go back to their snapshot values, or are removed."""
    fmt, f = _open(path)
    old: dict[str, list[str]] = snapshot["tags"]
    for native in _natives(fmt).values():
        if native in old:
            _set(fmt, f, native, old[native])
        else:
            _del(fmt, f, native)
    f.save()
