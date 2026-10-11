"""Re-importing the old library (docs/design/migration.md).

Preflight walks the read-only source once: hashes every file into a manifest and groups the
files into albums. Confirming turns each group into a request with one low-priority VERIFY job,
so the normal pipeline identifies, imports or sends it to review. The check job re-hashes the
source later to prove it is byte-identical. The source is only ever opened for reading.
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import stat
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.pipeline import Handler
from mixsync.acquire.requests import VERIFY_ATTEMPTS, candidate_dict
from mixsync.core.clock import Clock
from mixsync.core.jobs import JobKind
from mixsync.core.matching import Candidate, CandidateFile
from mixsync.db.models.library import Track
from mixsync.db.models.matching import MatchDecision
from mixsync.db.models.safety import FileOp
from mixsync.db.models.work import Job, Migration, Request
from mixsync.db.queue import JobRecord, enqueue_in
from mixsync.library import tags
from mixsync.match.candidates import DISC_DIR

log = logging.getLogger(__name__)

AUDIO = frozenset({".mp3", ".flac", ".ogg", ".m4a"})  # what library.tags can read and write
SPACE_FACTOR = 1.1  # free space needed on the library, times the source size
AID_RATE = 3.0  # AcoustID lookups per second
LISTED = 100  # entries kept of each list in a check result
MIGRATION_PRIORITY = 1  # normal requests (0) run first
MB_REQUESTS_TAGGED, MB_REQUESTS_UNTAGGED = 1, 4  # a fetch; or a search plus three fetches


@dataclass(frozen=True)
class Group:
    folder: str  # absolute album folder
    artist: str
    album: str
    mbid: str  # the musicbrainz_albumid tag, or ""
    files: list[tuple[str, int]]  # path relative to the folder, size


def _skip(rel: Path, why: str) -> dict[str, str]:
    return {"path": str(rel), "why": why}


def _files(root: Path, skipped: list[dict[str, str]]) -> Iterator[Path]:
    """Every regular file, whatever its type: the byte-identical check covers the whole tree.
    Symlinks are never followed; what cannot be read or is not a regular file goes in `skipped`
    as {"path", "why"}: paths may contain ": ", so never one string."""

    def onerror(e: OSError) -> None:
        skipped.append(_skip(Path(e.filename or root).relative_to(root), "cannot read folder"))

    for base, dirs, names in os.walk(root, onerror=onerror):
        dirs.sort()
        for d in dirs:
            if (p := Path(base) / d).is_symlink():
                skipped.append(_skip(p.relative_to(root), "symlink skipped"))
        for n in sorted(names):
            p = Path(base) / n
            try:
                mode = os.lstat(p).st_mode
            except OSError as e:
                skipped.append(_skip(p.relative_to(root), e.strerror or str(e)))
                continue
            if stat.S_ISREG(mode):
                yield p
            else:
                why = "symlink skipped" if stat.S_ISLNK(mode) else "not a regular file, skipped"
                skipped.append(_skip(p.relative_to(root), why))


def _hash(path: Path) -> tuple[int, str]:
    h, size = hashlib.sha256(), 0
    with path.open("rb") as f:  # read-only, always
        while chunk := f.read(1 << 20):
            h.update(chunk)
            size += len(chunk)
    return size, h.hexdigest()


def _album_dir(path: Path) -> Path:
    """A disc subfolder (`CD1`, `Disc 2`) belongs to the album folder above it."""
    return path.parent.parent if DISC_DIR.match(path.parent.name) else path.parent


def scan(root: Path, out: Path) -> dict[str, Any]:
    """Hash and group the source. Writes `manifest.jsonl` and `groups.json` in `out`."""
    out.mkdir(parents=True, exist_ok=True)
    unreadable: list[dict[str, str]] = []
    by_dir: dict[Path, list[tuple[Path, int, dict[str, str]]]] = {}
    count = total = 0
    with (out / "manifest.jsonl").open("w") as manifest:
        for path in _files(root, unreadable):
            rel = path.relative_to(root)
            try:
                size, digest = _hash(path)
            except OSError as e:
                unreadable.append(_skip(rel, e.strerror or str(e)))
                continue
            manifest.write(json.dumps({"path": str(rel), "size": size, "sha256": digest}) + "\n")
            count, total = count + 1, total + size
            if path.suffix.lower() not in AUDIO:
                continue
            try:
                tg = tags.read_fields(path)
            except Exception as e:  # corrupt or unsupported: it could not be imported either
                unreadable.append(_skip(rel, str(e)))
                continue
            by_dir.setdefault(_album_dir(path), []).append((path, size, tg))
    groups: list[Group] = []
    for folder, items in by_dir.items():
        albums = {tg.get("album", "") for _, _, tg in items}
        for album in sorted(albums) if len(albums) > 1 else [None]:  # loose files: one per album
            part = [i for i in items if album is None or i[2].get("album", "") == album]
            ids = Counter(v for _, _, tg in part if (v := tg.get("musicbrainz_albumid")))
            first = next((tg for _, _, tg in part if tg.get("album")), part[0][2])
            groups.append(
                Group(
                    str(folder),
                    first.get("albumartist") or first.get("artist", ""),
                    first.get("album", ""),
                    ids.most_common(1)[0][0] if ids else "",
                    [(str(p.relative_to(folder)), n) for p, n, _ in part],
                )
            )
    (out / "groups.json").write_text(json.dumps([asdict(g) for g in groups]))
    return {"files": count, "bytes": total, "unreadable": unreadable, "groups": groups}


def eta_seconds(groups: list[Group], mb_rate: float) -> float:
    """MusicBrainz requests, then AcoustID lookups: a job does them one after the other."""
    mb = sum(MB_REQUESTS_TAGGED if g.mbid else MB_REQUESTS_UNTAGGED for g in groups)
    return mb / mb_rate + sum(len(g.files) for g in groups) / AID_RATE


def _manifest(path: Path) -> dict[str, tuple[int, str]]:
    out: dict[str, tuple[int, str]] = {}
    for line in path.read_text().splitlines():
        e = json.loads(line)
        out[e["path"]] = (e["size"], e["sha256"])
    return out


def compare(root: Path, manifest: Path) -> dict[str, Any]:
    old, now = _manifest(manifest), set[str]()
    changed: list[str] = []
    for path in _files(root, []):  # what vanished or cannot be read shows as missing or changed
        rel = str(path.relative_to(root))
        now.add(rel)
        try:
            same = rel not in old or _hash(path) == old[rel]
        except OSError:
            same = False
        if not same:
            changed.append(rel)
    missing, added = sorted(old.keys() - now), sorted(now - old.keys())
    return {
        "identical": not (changed or missing or added),
        "changed": changed[:LISTED],
        "missing": missing[:LISTED],
        "added": added[:LISTED],
        "counts": {"changed": len(changed), "missing": len(missing), "added": len(added)},
    }


def start_preflight(s: Session, now: datetime, user_id: int) -> Migration:
    m = Migration(user_id=user_id)
    s.add(m)
    s.flush()
    enqueue_in(s, now, JobKind.MIGRATE_PREFLIGHT, {"migration_id": m.id})
    return m


def groups_of(m: Migration) -> list[Group]:
    raw = json.loads(Path(m.manifest_path).with_name("groups.json").read_text())
    return [
        Group(g["folder"], g["artist"], g["album"], g["mbid"], [(f[0], f[1]) for f in g["files"]])
        for g in raw
    ]


def confirm(s: Session, now: datetime, m: Migration) -> int:
    """One request and one VERIFY job per album. Does nothing unless the preflight passed, so a
    second confirm adds nothing."""
    claimed = cast(
        CursorResult[Any],
        s.execute(
            update(Migration)
            .where(Migration.id == m.id, Migration.status == "ready")
            .values(status="running")
        ),
    )
    s.expire(m)
    if claimed.rowcount != 1:  # lost a race to another confirm, or not ready
        return 0
    groups = groups_of(m)
    for i, g in enumerate(groups):
        req = Request(
            user_id=m.user_id,
            release_mbid="",
            artist=g.artist[:255],
            album=(g.album or Path(g.folder).name)[:255],
            status="verifying",
            migration_id=m.id,
        )
        s.add(req)
        s.flush()
        files = tuple(
            CandidateFile(rel, size, Path(rel).suffix.lower().lstrip(".")) for rel, size in g.files
        )
        enqueue_in(
            s,
            now,
            JobKind.VERIFY,
            {
                "request_id": req.id,
                "release_mbid": None,
                "candidate": candidate_dict(Candidate("", g.folder, files)),
                "paths": [str(Path(g.folder) / rel) for rel, _ in g.files],
            },
            max_attempts=VERIFY_ATTEMPTS,
            priority=MIGRATION_PRIORITY,
            idempotency_key=f"migrate:{m.id}:{i}",
        )
    return len(groups)


def retry(s: Session, now: datetime, req: Request) -> str | None:
    """Send a failed or skipped album on again. With files already in the library, import that
    same decision again (it skips what is in); else re-match from the first VERIFY, so it lands
    in review (or imports) with fresh choices. Returns why it cannot, else None."""
    batch = s.scalar(
        select(Track.batch_id).where(Track.batch_id.like(f"request-{req.id}-decision-%")).limit(1)
    )
    copied = s.scalar(
        select(FileOp.id)
        .where(
            FileOp.batch_id.like(f"request-{req.id}-decision-%"),
            FileOp.kind == "copy",
            FileOp.status == "done",
            FileOp.src != "(generated)",
        )
        .limit(1)
    )
    if batch is None and copied is not None:  # a re-match could copy back what a merge removed
        return "its imported files were later merged or removed"
    first = s.scalar(
        select(Job)
        .where(Job.kind == JobKind.VERIFY, Job.payload["request_id"].as_integer() == req.id)
        .order_by(Job.id)
        .limit(1)
    )
    if batch is None and first is None:
        return "it was never matched"
    values = (
        {"status": "importing", "last_error": None}
        if batch
        else {"status": "verifying", "release_mbid": "", "last_error": None}
    )
    claimed = cast(
        CursorResult[Any],
        s.execute(
            update(Request)
            .where(Request.id == req.id, Request.status.in_(("failed", "skipped")))
            .values(values)
        ),
    )
    s.expire(req)
    if claimed.rowcount != 1:  # not failed (any more): a double click retries once
        return "it has not failed"
    if batch:  # no idempotency key: the first import's job exists already
        payload = {"request_id": req.id, "decision_id": int(batch.rpartition("-")[2])}
        enqueue_in(s, now, JobKind.IMPORT, payload, priority=MIGRATION_PRIORITY)
    elif first:
        enqueue_in(
            s,
            now,
            JobKind.VERIFY,
            {**first.payload, "release_mbid": None},
            max_attempts=VERIFY_ATTEMPTS,
            priority=MIGRATION_PRIORITY,
        )
    return None


def report(s: Session, m: Migration) -> list[dict[str, Any]]:
    """One row per album folder, then one per file that could not be read."""
    reqs = list(s.scalars(select(Request).where(Request.migration_id == m.id).order_by(Request.id)))
    ids = {r.id for r in reqs}
    first: dict[int, dict[str, Any]] = {}  # the VERIFY job each request started with
    for j in s.scalars(select(Job).where(Job.kind == JobKind.VERIFY).order_by(Job.id)):
        if j.payload["request_id"] in ids:
            first.setdefault(j.payload["request_id"], j.payload)
    partial: dict[int, str] = {}  # latest decision per request wins: a retry may be complete
    for d in s.scalars(select(MatchDecision).order_by(MatchDecision.id)):
        if d.evidence.get("request_id") in ids:
            partial[d.evidence["request_id"]] = d.evidence.get("partial", "")
    rows = [
        {
            "folder": first.get(r.id, {}).get("candidate", {}).get("folder", ""),
            "status": "partial" if r.status == "imported" and partial.get(r.id) else r.status,
            "artist": r.artist,
            "album": r.album,
            "release_mbid": r.release_mbid,
            "files": len(first.get(r.id, {}).get("paths", [])),
            "reason": (r.status == "imported" and partial.get(r.id)) or r.last_error or "",
            "request_id": r.id,
        }
        for r in reqs
    ]
    root = m.summary.get("source", "")
    for u in m.summary.get("unreadable", []):
        rel, why = u["path"], u["why"]
        rows.append(
            {
                "folder": str((Path(root) / rel).parent),
                "status": "failed",
                "artist": "",
                "album": "",
                "release_mbid": "",
                "files": 1,
                "request_id": None,
                "reason": f"{Path(rel).name}: {why}",
            }
        )
    return rows


def totals(rows: list[dict[str, Any]]) -> Counter[str]:
    c = Counter(r["status"] for r in rows)
    c["in progress"] = c.pop("verifying", 0) + c.pop("importing", 0)
    return c


@dataclass(frozen=True)
class Migrator:
    sessions: sessionmaker[Session]
    clock: Clock
    source: Path
    config_dir: Path
    library: Path
    mb_rate: float

    def handlers(self) -> dict[JobKind, Handler]:
        return {JobKind.MIGRATE_PREFLIGHT: self.preflight, JobKind.MIGRATE_CHECK: self.check}

    async def preflight(self, job: JobRecord) -> None:
        mid = job.payload["migration_id"]
        out = self.config_dir / "migrations" / str(mid)
        summary: dict[str, Any] = {"source": str(self.source)}
        with self.sessions() as s:
            m = s.get(Migration, mid)
            if m is None or m.status != "preflight":
                return  # a retry of a finished job must not rescan or reset anything
        scanned = False
        try:
            if not self.source.is_dir():
                raise ValueError(f"{self.source} is not a folder")
            found = await asyncio.to_thread(scan, self.source, out)  # the heartbeat keeps running
            scanned = True
            groups: list[Group] = found["groups"]
            free = shutil.disk_usage(self.library if self.library.exists() else self.library.parent)
            need = int(found["bytes"] * SPACE_FACTOR)
            summary |= {
                "files": found["files"],
                "bytes": found["bytes"],
                "unreadable": found["unreadable"],
                "albums": len(groups),
                "tagged_albums": sum(bool(g.mbid) for g in groups),
                "free": free.free,
                "required": need,
                "eta_seconds": eta_seconds(groups, self.mb_rate),
            }
            if not groups:
                summary["reason"] = "no readable audio files found"
            elif free.free < need:
                summary["reason"] = f"{free.free} bytes free on the library, {need} needed"
        except Exception as e:  # the page shows why; the admin starts a new preflight
            log.exception("migration %s preflight failed", mid)
            summary["reason"] = str(e)
        with self.sessions.begin() as s:
            m = s.get(Migration, mid)
            if m is None or m.status != "preflight":
                return
            m.summary = summary
            if scanned:  # a partial manifest would make the check lie
                m.manifest_path = str(out / "manifest.jsonl")
            m.status = "failed" if "reason" in summary else "ready"

    async def check(self, job: JobRecord) -> None:
        mid = job.payload["migration_id"]
        with self.sessions() as s:
            m = s.get(Migration, mid)
            if m is None:
                raise LookupError(f"migration {mid} does not exist")
            manifest = Path(m.manifest_path)
        result = await asyncio.to_thread(compare, self.source, manifest)
        with self.sessions.begin() as s:
            m = s.get(Migration, mid)
            if m:
                m.verify = {"checked_at": self.clock.now().isoformat(), **result}
