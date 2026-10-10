"""Release groups split across several releases, and the dry-run plan that merges them."""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mixsync.core.matching import AlbumInfo, TrackInfo
from mixsync.db.models.library import CanonicalRelease, Track
from mixsync.library import canonical, paths, tags
from mixsync.library.importer import render_path, tagset_for


@dataclass(frozen=True)
class Fragment:
    group_mbid: str
    canonical: str | None  # the stored canonical release, if any
    releases: dict[str, int]  # release mbid -> verified tracks filed under it


def find(s: Session) -> list[Fragment]:
    """Groups whose verified tracks span several releases or sit off the stored canonical one."""
    rows = s.execute(
        select(Track.release_group_mbid, Track.release_mbid, func.count())
        .where(
            Track.status == "verified",
            Track.release_group_mbid.is_not(None),
            Track.release_mbid.is_not(None),
        )
        .group_by(Track.release_group_mbid, Track.release_mbid)
    ).all()
    groups: dict[str, dict[str, int]] = defaultdict(dict)
    for group, release, n in rows:
        groups[str(group)][str(release)] = n
    stored = {c.release_group_mbid: c.release_mbid for c in s.scalars(select(CanonicalRelease))}
    return [
        Fragment(g, stored.get(g), rels)
        for g, rels in sorted(groups.items())
        if len(rels) > 1 or (g in stored and stored[g] not in rels)
    ]


def row_dict(t: Track) -> dict[str, Any]:
    """Every Track field, so a plan can put the row back exactly as it was."""
    return {
        "id": t.id,
        "path": t.path,
        "recording_mbid": t.recording_mbid,
        "release_mbid": t.release_mbid,
        "release_group_mbid": t.release_group_mbid,
        "artist_mbids": list(t.artist_mbids),
        "acoustid_id": t.acoustid_id,
        "status": t.status,
        "batch_id": t.batch_id,
        "imported_at": t.imported_at.isoformat(),
    }


def tagset_from_dict(d: dict[str, Any]) -> tags.TagSet:
    kw: dict[str, Any] = {
        k: tuple(v) if k in ("artist_ids", "albumartist_ids", "genres") else v for k, v in d.items()
    }
    return tags.TagSet(**kw)


def build_ops(
    group_tracks: list[Track],
    canon: AlbumInfo,
    template: str,
    exists: Callable[[str], bool],
    size: Callable[[str], int] = lambda _: 0,
) -> list[dict[str, Any]]:
    """What merging the group onto `canon` would do to each verified track not on it:
    `trash` (the canonical release already has that recording), `move` (retag and refile under
    the canonical track) or `keep` (not on the canonical release). No two ops share a target
    path, and a taken path gets a suffix. `exists` says whether a library path is taken."""
    tracks = sorted((t for t in group_tracks if t.status == "verified"), key=lambda t: t.path)
    keepers = {
        t.recording_mbid: t
        for t in tracks
        if t.release_mbid == canon.release_id and t.recording_mbid
    }
    ops: list[tuple[Track, dict[str, Any]]] = []
    rest: list[Track] = []
    for t in tracks:
        if t.release_mbid == canon.release_id:
            continue
        if t.recording_mbid in keepers:
            ops.append((t, {"op": "trash", "keeper": keepers[t.recording_mbid].path}))
        else:
            rest.append(t)
    rest.sort(key=lambda t: -size(t.path))  # of several copies of a recording, keep the biggest
    by_path = {t.path: t for t in rest}
    mapped, bonus = canonical.split(
        [(Path(t.path), TrackInfo("", recording_id=t.recording_mbid), t.acoustid_id) for t in rest],
        canon.tracks,
    )
    taken: set[str] = set()
    moved: dict[str, str] = {}  # recording -> the path of its one copy
    for path, ctrack, acoustid in mapped:
        t = by_path[str(path)]
        ts = tagset_for(ctrack, canon, acoustid, t.status)
        # the track's own path is free: its original is trashed before the copy is made
        new = paths.unique(
            render_path(template, ts, path.suffix),
            lambda r, own=t.path: r in taken or (r != own and exists(r)),
        )
        taken.add(new)
        moved.setdefault(str(t.recording_mbid), new)
        ops.append((t, {"op": "move", "new_path": new, "tags": asdict(ts)}))
    for path, track, _ in bonus:  # one copy only: extras of a moved recording are trashed
        keeper = moved.get(str(track.recording_id))
        extra = {"op": "trash", "keeper": keeper} if keeper else {"op": "keep"}
        ops.append((by_path[str(path)], extra))
    return [{**o, "track_id": t.id, "old": row_dict(t), "done": False} for t, o in ops]
