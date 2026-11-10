"""The Spotify-file upstream: a mirrored playlist fed by an uploaded Spotify playlist file. No
Spotify API call. The last uploaded track list is the stored upstream state: a JSON file under
`<config_dir>/spotify/<user_id>/<ref>.json` (no schema change; the worker and web share
config_dir). Uploading again to the same playlist name replaces the file and runs the usual sync.

Formats (field names from Spotify's "Download your data" Playlist1.json as seen in public
samples, and the exportify CSV, https://github.com/watsonbox/exportify; neither source is a spec,
so unknown extras are ignored and only the fields used here are required):
- JSON: {"playlists": [{"name", "items": [{"track": {"trackName", "artistName", "albumName",
  "trackUri"}}]}]}. Local files and episodes have no "track" object and are skipped. No ISRC.
- CSV: header with "Track Name" and "Artist Name(s)" (or "Artist Name"); "ISRC" optional.
  Several artists are joined by ";": the first is used. The playlist is named after the file."""

import csv
import hashlib
import io
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy.orm import Session

from mixsync.core.errors import PermanentError
from mixsync.db.models.discovery import MirroredPlaylist
from mixsync.discovery import mirrored
from mixsync.discovery.mirrored import UpstreamTrack

KIND = "spotify_file"
MAX_BYTES = 5_000_000
Parsed = dict[str, list[UpstreamTrack]]  # playlist name -> tracks, in file order


class BadFile(ValueError):
    """The upload is not a Spotify playlist file we can read; the message is for the user."""


def _track(artist: object, title: object, isrc: object = None) -> UpstreamTrack | None:
    a, t = str(artist or "").strip(), str(title or "").strip()
    i = str(isrc or "").strip().upper()
    return UpstreamTrack(a, t, i or None) if a and t else None


def _obj(x: object) -> dict[str, Any]:
    return cast(dict[str, Any], x) if isinstance(x, dict) else {}


def _from_json(doc: object) -> Parsed:
    lists = _obj(doc).get("playlists")
    if not isinstance(lists, list) or not lists:
        raise BadFile('not a Spotify playlist export: no "playlists" list')
    out: Parsed = {}
    for p in cast(list[Any], lists):
        items, name = _obj(p).get("items"), str(_obj(p).get("name") or "").strip()
        if not isinstance(items, list) or not name:
            raise BadFile("not a Spotify playlist export: a playlist has no name or items")
        tracks: list[UpstreamTrack] = []
        for item in cast(list[Any], items):
            t = _obj(_obj(item).get("track"))
            if tr := _track(t.get("artistName"), t.get("trackName"), t.get("isrc")):
                tracks.append(tr)
        out.setdefault(name[:255], tracks)
    return out


def _from_csv(text: str, name: str) -> Parsed:
    rows = csv.DictReader(io.StringIO(text))
    fields = rows.fieldnames or []
    artist = next((f for f in ("Artist Name(s)", "Artist Name") if f in fields), None)
    if "Track Name" not in fields or artist is None:
        raise BadFile('not a Spotify playlist CSV: needs "Track Name" and "Artist Name(s)" columns')
    tracks = [
        tr
        for r in rows
        if (tr := _track((r[artist] or "").split(";")[0], r["Track Name"], r.get("ISRC")))
    ]
    return {name[:255]: tracks}


def parse(data: bytes, filename: str) -> Parsed:
    """The playlists in an uploaded file. Raises BadFile; nothing is stored here."""
    if len(data) > MAX_BYTES:
        raise BadFile(f"file is larger than {MAX_BYTES // 1_000_000} MB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise BadFile("file is not UTF-8 text") from None
    if text.lstrip().startswith(("{", "[")):
        try:
            return _from_json(json.loads(text))
        except json.JSONDecodeError as e:
            raise BadFile(f"invalid JSON: {e.msg}") from None
    return _from_csv(text, Path(filename).stem.strip() or "Spotify playlist")


def ref_for(name: str) -> str:
    return hashlib.sha256(name.casefold().encode()).hexdigest()[:32]


class SpotifyFile:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, user_id: int, ref: str) -> Path:
        return self.root / str(user_id) / f"{ref}.json"

    async def tracks(self, user_id: int, ref: str) -> list[UpstreamTrack]:
        try:
            rows = json.loads(self._path(user_id, ref).read_text("utf-8"))
        except (OSError, ValueError):
            raise PermanentError(
                "the uploaded track list is missing; upload the file again"
            ) from None
        return [UpstreamTrack(r["artist"], r["title"], r["isrc"]) for r in rows]

    def save(self, user_id: int, ref: str, tracks: list[UpstreamTrack]) -> bool:
        """Store the list; False if it is already the stored one."""
        path = self._path(user_id, ref)
        body = json.dumps([{"artist": t.artist, "title": t.title, "isrc": t.isrc} for t in tracks])
        if path.exists() and path.read_text("utf-8") == body:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(body, "utf-8")
        os.replace(tmp, path)
        return True

    def upload(
        self, s: Session, now: datetime, user_id: int, name: str, tracks: list[UpstreamTrack]
    ) -> MirroredPlaylist:
        """The user's mirror of playlist `name`, made on the first upload; a changed list is
        stored and synced now, the same list again does nothing. A name the user already uses
        for another mirror raises IntegrityError."""
        ref = ref_for(name)
        pl = mirrored.create(s, now, user_id, name, KIND, ref)
        if self.save(user_id, ref, tracks):
            mirrored.sync_now(s, now, pl, force=True)
        return pl
