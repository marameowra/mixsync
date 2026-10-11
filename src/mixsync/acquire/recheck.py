"""Re-checking an unverified album against MusicBrainz, and the draft plan that promotes it.

One RECHECK job per album: one search, at most three releases, one AcoustID lookup per file.
A confident match only stores a draft `promote` plan (same op format as a merge `move`); the
user confirms it and the Merger runs it, so nothing changes on disk without them. Without a
confident match a background check schedules the next one, backing off (see requests.py).
"""

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.merge import live_job
from mixsync.acquire.pipeline import Handler, acoustid_id, file_track, likelies
from mixsync.acquire.requests import AlbumKey, enqueue_recheck, profile_for
from mixsync.core.clock import Clock
from mixsync.core.jobs import JobKind, JobState
from mixsync.core.matching import AlbumInfo, Band, FileTrack, Profile
from mixsync.core.protocols import AcoustIdLookup, Fingerprinter, MetadataProvider
from mixsync.db.models.library import Plan, Track
from mixsync.db.models.work import Job
from mixsync.db.queue import JobRecord
from mixsync.library import paths, tags
from mixsync.library.fileops import FileOps
from mixsync.library.fragmentation import row_dict
from mixsync.library.importer import render_path, tagset_for
from mixsync.match.profiles import BALANCED
from mixsync.match.scorer import assign_tracks, score_files

SEARCH_LIMIT, MAX_RELEASES = 5, 3

Item = tuple[Track, dict[str, str]]


def read_unverified(db: Session, root: Path) -> list[Item]:
    # shortcut: reads the tags of every unverified file per call; move the album fields
    # into DB columns when unverified sets get large.
    rows = db.scalars(select(Track).where(Track.status == "unverified").order_by(Track.id)).all()
    items: list[Item] = []
    for t in rows:
        try:
            items.append((t, tags.read_fields(root / t.path)))
        except Exception:  # file moved or unreadable: the path alone still identifies it
            items.append((t, {}))
    return items


def _year(tg: dict[str, str]) -> str:
    y = tg.get("date", "")[:4]
    return y if y.isascii() and y.isdigit() and y != "0000" else ""


def album_key(tg: dict[str, str]) -> AlbumKey:
    return tg.get("albumartist") or tg.get("artist", ""), tg.get("album", ""), _year(tg)


def group(items: list[Item]) -> dict[AlbumKey, list[Item]]:
    """Group by tags, not import batch (a batch can mix albums). A year-less group joins the one
    dated group with the same artist and album, if there is exactly one."""
    albums: dict[AlbumKey, list[Item]] = {}
    for it in items:
        albums.setdefault(album_key(it[1]), []).append(it)
    for key in [k for k in albums if not k[2]]:
        dated = [k for k in albums if k[:2] == key[:2] and k[2]]
        if len(dated) == 1:
            albums[dated[0]] += albums.pop(key)
    return albums


@dataclass(frozen=True)
class Status:
    last: datetime | None  # the last finished check
    next: datetime | None  # the next background check
    plan_id: int | None  # a draft promote plan waiting for the user


def promote_plans(s: Session, *states: str) -> list[Plan]:
    return list(s.scalars(select(Plan).where(Plan.kind == "promote", Plan.status.in_(states))))


def _has(plan: Plan, ids: set[int]) -> bool:
    return bool(ids & {o["track_id"] for o in plan.ops})


def statuses(s: Session, albums: dict[AlbumKey, list[Item]]) -> dict[AlbumKey, Status]:
    jobs = list(s.scalars(select(Job).where(Job.kind == JobKind.RECHECK)))
    drafts = promote_plans(s, "draft")
    out: dict[AlbumKey, Status] = {}
    for key, items in albums.items():
        mine = [j for j in jobs if tuple(j.payload["album"]) == key]
        done = [j.updated_at for j in mine if j.status is JobState.SUCCEEDED]
        due = [
            j.run_after for j in mine if j.status is JobState.QUEUED and not j.payload["user_id"]
        ]
        ids = {t.id for t, _ in items}
        plan = [p.id for p in drafts if _has(p, ids)]
        out[key] = Status(max(done, default=None), min(due, default=None), max(plan, default=None))
    return out


@dataclass(frozen=True)
class Rechecker:
    sessions: sessionmaker[Session]
    clock: Clock
    metadata: MetadataProvider
    fingerprint: Fingerprinter
    acoustid: AcoustIdLookup
    fileops: FileOps
    template: str

    def handlers(self) -> dict[JobKind, Handler]:
        return {JobKind.RECHECK: self.recheck}

    async def recheck(self, job: JobRecord) -> None:
        key: AlbumKey = (*job.payload["album"],)  # pyright: ignore[reportAssignmentType]
        uid, attempt = job.payload["user_id"], job.payload["attempt"]
        with self.sessions() as s:
            items = group(read_unverified(s, self.fileops.library)).get(key, [])
            profile = profile_for(s, uid) if uid else BALANCED
        items = [i for i in items if self.fileops.has(i[0].path)]
        if not items or not key[1]:  # nothing unverified left: the chain ends
            return
        found = await self._match(key, items, profile)
        with self.sessions.begin() as s:
            if found:
                self._draft(s, items, *found)
            elif uid is None:  # a user's check leaves the background schedule alone
                chain = f"recheck:next:{job.id}"
                now = self.clock.now()
                enqueue_recheck(s, now, key, None, attempt=attempt + 1, chain_key=chain)

    async def _match(
        self, key: AlbumKey, items: list[Item], profile: Profile
    ) -> tuple[AlbumInfo, list[FileTrack]] | None:
        """The best of the top releases, if it is confident enough to promote to."""
        refs = await self.metadata.search_releases(key[0], key[1], limit=SEARCH_LIMIT)
        albums = [await self.metadata.get_release(r.release_id) for r in refs[:MAX_RELEASES]]
        if not albums:
            return None
        files: list[FileTrack] = []
        for t, _ in items:
            path = self.fileops.library / t.path
            fp = await self.fingerprint(path)
            files.append(file_track(path, fp.duration, tuple(await self.acoustid.lookup(fp))))
        like = likelies([self.fileops.library / t.path for t, _ in items])
        scored = [(score_files(files, like, a, profile), a) for a in albums]
        result, best = min(scored, key=lambda x: x[0].distance)
        return (best, files) if result.band is Band.AUTO_ACCEPT else None

    def _draft(
        self, s: Session, items: list[Item], album: AlbumInfo, files: list[FileTrack]
    ) -> None:
        rows = [t for t, _ in items]
        ids = {t.id for t in rows}
        mine = [p for p in promote_plans(s, "draft", "running", "undoing") if _has(p, ids)]
        if any(p.status != "draft" or live_job(s, p.id) for p in mine):
            return  # a plan for this album is confirmed or running: leave it be
        pairs, _, _ = assign_tracks(files, album.tracks)
        paired = {id(f): t for f, t in pairs}
        in_db, taken = set(s.scalars(select(Track.path))), set[str]()
        ops: list[dict[str, Any]] = []
        for row, f in zip(rows, files, strict=True):
            op: dict[str, Any] = {"op": "keep"}  # not on the release: stays unverified
            if id(f) in paired:
                ts = tagset_for(paired[id(f)], album, acoustid_id(f), "verified")
                new = paths.unique(
                    render_path(self.template, ts, Path(row.path).suffix),
                    # the track's own path frees up: its original is trashed first
                    lambda r, own=row.path: (
                        r in taken or (r != own and (r in in_db or self.fileops.has(r)))
                    ),
                )
                taken.add(new)
                op = {"op": "move", "new_path": new, "tags": asdict(ts)}
            ops.append({**op, "track_id": row.id, "old": row_dict(row), "done": False})
        for p in mine:  # abandoned previews: never executed, so DB only
            p.status = "discarded"
        plan = Plan(
            kind="promote",
            release_group_mbid=album.release_group_id or "",
            canonical_release_mbid=album.release_id or "",
            ops=ops,
        )
        s.add(plan)
        s.flush()
        plan.batch_id = f"promote-{plan.id}"
