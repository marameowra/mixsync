"""The acquire job chain: search -> download -> verify -> import (or review) -> rescan.

Every handler reads what it needs, does its I/O outside any transaction, then writes its result
and enqueues the next job in one transaction. The next job's idempotency key is derived from the
current job's id, so a handler re-run after a crash never forks the chain.
"""

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.requests import (
    VERIFY_ATTEMPTS,
    AlbumKey,
    candidate_dict,
    candidate_from_dict,
    enqueue_import,
    enqueue_recheck,
    enqueue_rescan,
    profile_for,
    retry_search,
)
from mixsync.core.clock import Clock
from mixsync.core.jobs import JobKind, JobState
from mixsync.core.matching import (
    AlbumInfo,
    Band,
    Candidate,
    FileTrack,
    Likelies,
    TrackInfo,
    TransferStatus,
)
from mixsync.core.protocols import (
    AcoustIdLookup,
    CoverArt,
    DownloadSource,
    Fingerprinter,
    LibraryTarget,
    MetadataProvider,
)
from mixsync.db import canonical as canonical_db
from mixsync.db import decisions
from mixsync.db.models.work import Job, Request
from mixsync.db.queue import JobRecord, enqueue_in
from mixsync.library import canonical, tags
from mixsync.library.importer import Importer, ImportResult
from mixsync.match.candidates import parse_name, rank
from mixsync.match.scorer import assign_tracks, score_files

log = logging.getLogger(__name__)

Handler = Callable[[JobRecord], Awaitable[None]]

SEARCH_DEBOUNCE = timedelta(minutes=10)  # one request never searches twice within this
NO_RESULTS_BACKOFF = (
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(hours=24),
    timedelta(weeks=1),
)
MAX_SEARCHES = 2
SEARCH_BUSY_DELAY = timedelta(minutes=1)
MONITOR_EVERY = timedelta(seconds=30)
DOWNLOAD_STALL = timedelta(hours=2)  # no new bytes for this long -> the peer has failed us
ACOUSTID_ID_SCORE = 0.9  # an AcoustID id is written to the tags only above this


@dataclass(frozen=True)
class Pipeline:
    sessions: sessionmaker[Session]
    clock: Clock
    source: DownloadSource
    queries: Callable[[str, str], list[str]]  # sources.query.queries; injected (adapter module)
    metadata: MetadataProvider
    fingerprint: Fingerprinter
    acoustid: AcoustIdLookup
    importer: Importer
    coverart: CoverArt
    target: LibraryTarget
    canonical_media: tuple[str, ...] = ("Digital Media", "CD")
    canonical_countries: tuple[str, ...] = ("XW",)

    def handlers(self) -> dict[JobKind, Handler]:
        return {
            JobKind.SEARCH: self.search,
            JobKind.DOWNLOAD: self.download,
            JobKind.VERIFY: self.verify,
            JobKind.IMPORT: self.import_,
            JobKind.RESCAN: self.rescan,
        }

    def _request(self, s: Session, job: JobRecord) -> Request:
        req = s.get(Request, job.payload["request_id"])
        if req is None:
            raise LookupError(f"request {job.payload['request_id']} does not exist")
        return req

    async def search(self, job: JobRecord) -> None:
        now = self.clock.now()
        with self.sessions.begin() as s:
            req = self._request(s, job)
            if req.status not in ("searching", "no_results"):
                return  # superseded: another job already moved this request on
            wait = req.next_search_at and _aware(req.next_search_at, now)
            # A DB count is enough: the cap protects the peer network, not a hard invariant.
            busy = s.scalar(
                select(func.count(Job.id)).where(
                    Job.kind == JobKind.SEARCH, Job.status == JobState.RUNNING, Job.id != job.id
                )
            )
            if (wait and wait > now) or (busy or 0) >= MAX_SEARCHES:
                later = wait if wait and wait > now else now + SEARCH_BUSY_DELAY
                enqueue_in(
                    s,
                    now,
                    JobKind.SEARCH,
                    job.payload,
                    run_after=later,
                    idempotency_key=f"search:defer:{job.id}",
                )
                return
            mbid, tried, profile = req.release_mbid, req.tried, profile_for(s, req.user_id)

        album = await self.metadata.get_release(mbid)
        if album.release_group_id and album.tracks:
            with self.sessions() as s:
                have = canonical_db.owned(
                    s, album.release_group_id, [t.recording_id or "" for t in album.tracks]
                )
            if all(t.recording_id in have for t in album.tracks):  # nothing left to fetch
                with self.sessions.begin() as s:
                    req = self._request(s, job)
                    req.artist, req.album = album.artist[:255], album.album[:255]
                    req.status, req.last_error = "owned", None
                return
        ranked: list[tuple[Candidate, Any]] = []
        for q in self.queries(album.artist, album.album):
            found = [c for c in await self.source.search(q) if [c.username, c.folder] not in tried]
            ranked = [cr for cr in rank(found, album, profile) if cr[1].band is not Band.REJECT]
            if ranked:
                break

        now = self.clock.now()
        with self.sessions.begin() as s:
            req = self._request(s, job)
            req.artist, req.album = album.artist[:255], album.album[:255]
            if not ranked:
                delay = NO_RESULTS_BACKOFF[
                    min(req.no_results_attempts, len(NO_RESULTS_BACKOFF) - 1)
                ]
                req.no_results_attempts += 1
                req.status, req.next_search_at = "no_results", now + delay
                enqueue_in(
                    s,
                    now,
                    JobKind.SEARCH,
                    job.payload,
                    run_after=now + delay,
                    idempotency_key=f"search:again:{job.id}",
                )
                return
            cand, result = ranked[0]
            decisions.record(
                s,
                result,
                evidence={"request_id": req.id, "source": _source(cand), "target": _target(album)},
                user_id=req.user_id,
                stage=1,
                release_mbid=mbid,
            )
            req.status, req.no_results_attempts = "downloading", 0
            req.next_search_at = now + SEARCH_DEBOUNCE
            enqueue_in(
                s,
                now,
                JobKind.DOWNLOAD,
                {"request_id": req.id, "candidate": candidate_dict(cand)},
                idempotency_key=f"download:{job.id}",
            )

    async def download(self, job: JobRecord) -> None:
        """First run enqueues the transfer; then each run is one status check that schedules
        the next as a fresh job, so a slow transfer never uses up the retry budget (SK-P05)."""
        p = job.payload
        cand = candidate_from_dict(p["candidate"])
        now = self.clock.now()
        if "bytes" not in p:
            await self.source.enqueue(cand)
            self._monitor(job, now, 0, now)
            return
        info = await self.source.status(cand)
        stalled = datetime.fromisoformat(p["since"])
        if info.state in (TransferStatus.QUEUED, TransferStatus.IN_PROGRESS):
            if info.bytes > p["bytes"]:
                stalled = now
            if now - stalled < DOWNLOAD_STALL:
                self._monitor(job, now, info.bytes, stalled)
                return
        with self.sessions.begin() as s:
            req = self._request(s, job)
            if info.state is not TransferStatus.DONE:
                why = "transfer failed" if info.state is TransferStatus.FAILED else "stalled"
                retry_search(s, now, req, cand.username, cand.folder, why)
                return
            problem = _missing(cand, info.local_paths)
            if problem:  # never import a missing or partial file (SK-P04)
                retry_search(s, now, req, cand.username, cand.folder, problem)
                return
            req.status = "verifying"
            enqueue_in(
                s,
                now,
                JobKind.VERIFY,
                {
                    "request_id": req.id,
                    "release_mbid": req.release_mbid,
                    "candidate": p["candidate"],
                    "paths": list(info.local_paths),
                },
                max_attempts=VERIFY_ATTEMPTS,
                idempotency_key=f"verify:{job.id}",
            )

    def _monitor(self, job: JobRecord, now: datetime, sent: int, since: datetime) -> None:
        with self.sessions.begin() as s:
            enqueue_in(
                s,
                now,
                JobKind.DOWNLOAD,
                {**job.payload, "bytes": sent, "since": since.isoformat()},
                run_after=now + MONITOR_EVERY,
                idempotency_key=f"monitor:{job.id}",
            )

    async def verify(self, job: JobRecord) -> None:
        """AcoustID is always consulted: if it is unreachable the job retries, never imports."""
        p = job.payload
        cand = candidate_from_dict(p["candidate"])
        paths = [Path(x) for x in p["paths"]]
        album = await self.metadata.get_release(p["release_mbid"])
        files: list[FileTrack] = []
        for path in paths:
            fp = await self.fingerprint(path)
            results = await self.acoustid.lookup(fp)
            files.append(file_track(path, fp.duration, tuple(results)))
        with self.sessions() as s:
            profile = profile_for(s, self._request(s, job).user_id)
        result = score_files(files, likelies(paths), album, profile)
        pairs, _, _ = assign_tracks(files, album.tracks)
        index = {id(f): i for i, f in enumerate(files)}
        track_no = {id(t): i for i, t in enumerate(album.tracks)}
        assignment = [
            {
                "local_path": str(paths[index[id(f)]]),
                "track": track_no[id(t)],
                "acoustid_id": acoustid_id(f),
            }
            for f, t in pairs
        ]
        evidence = {
            "request_id": p["request_id"],
            "candidate": p["candidate"],
            "source": _source(cand, paths),
            "target": _target(album),
            "acoustid": [_best(f) for f in files],
            "duration_deltas": [abs((f.length or 0) - (t.length or 0)) for f, t in pairs],
            "assignment": assignment,
        }

        now = self.clock.now()
        with self.sessions.begin() as s:
            req = self._request(s, job)
            d = decisions.record(
                s,
                result,
                evidence=evidence,
                user_id=req.user_id,
                stage=2,
                release_mbid=p["release_mbid"],
            )
            if result.band is Band.AUTO_ACCEPT:
                enqueue_import(s, now, req, d.id)
            elif result.band is Band.REVIEW:
                req.status = "review"
            else:  # files stay in /downloads: reject never deletes
                retry_search(s, now, req, cand.username, cand.folder, "rejected after download")

    async def import_(self, job: JobRecord) -> None:
        rid, did = job.payload["request_id"], job.payload["decision_id"]
        with self.sessions() as s:
            d = decisions.get(s, did)
            if d is None:
                raise LookupError(f"decision {did} does not exist")
            cand = candidate_from_dict(d.evidence["candidate"])
            edits = d.evidence.get("unverified")
            mbid = d.chosen_release_mbid or d.release_mbid
            assignment: list[dict[str, Any]] = [] if edits else d.evidence["assignment"]  # strict
            switched = "canonical_switched_from" in d.evidence  # an earlier run made us canonical
        if edits:
            return await self._import_unverified(job, cand, edits)
        album = await self.metadata.get_release(mbid)
        files: list[tuple[Path, TrackInfo, str | None]] = [
            (Path(a["local_path"]), album.tracks[a["track"]], a["acoustid_id"]) for a in assignment
        ]
        # Same batch id on every retry, so a resumed import skips what is already in.
        batch = f"request-{rid}-decision-{did}"
        target, n = album, len(files)
        while (cid := await self._canonical_id(album)) and cid != album.release_id:
            canon = await self.metadata.get_release(cid)
            mapped, rest = canonical.split(files, canon.tracks)
            if not mapped:  # no overlap at all: the canonical release is not this album
                break
            if not rest:
                target, files = canon, await self._unowned(album, mapped, batch)
                break
            # Bonus tracks: this release is the fuller edition, so it becomes canonical. Files
            # already under the old one stay put; the merge plan will deal with that split.
            own, group = str(album.release_id), str(album.release_group_id)
            with self.sessions.begin() as s:
                if canonical_db.switch(s, group, cid, own) == own:
                    # Same transaction: a retry must not mistake this for a plain canonical import.
                    row = decisions.get(s, did)
                    if row:
                        row.evidence = {**row.evidence, "canonical_switched_from": cid}
                    switched = True
                    break
            # lost a race to another switch: decide again against the stored choice
        if cid and cid == album.release_id and not switched:
            # shortcut: two workers importing the same release at once can still duplicate (rows
            # not yet committed); the fragmentation merge cleans that up.
            files = await self._unowned(album, files, batch)
        if n and not files:  # every file is a verified duplicate: nothing to import or rescan
            with self.sessions.begin() as s:
                req = self._request(s, job)
                req.status, req.last_error = "owned", None
            return
        results = await asyncio.to_thread(
            self.importer.import_release, files, target, "verified", batch
        )
        await self._cover(target, results, batch)
        await self._finish(job, cand, results, batch)

    async def _import_unverified(
        self, job: JobRecord, cand: Candidate, edits: dict[str, Any]
    ) -> None:
        """The reviewer's tags and no MusicBrainz or AcoustID ids (nothing vouches for the
        files). No canonical logic, duplicate check or cover art: all of those need ids."""
        album = AlbumInfo(edits["albumartist"], edits["album"], (), year=edits["year"])
        files: list[tuple[Path, TrackInfo, str | None]] = [
            (
                Path(t["local_path"]),
                TrackInfo(
                    t["title"], t["artist"] or None, medium_index=t["track"], medium=t["disc"]
                ),
                None,
            )
            for t in edits["tracks"]
        ]
        batch = f"request-{job.payload['request_id']}-decision-{job.payload['decision_id']}"
        results = await asyncio.to_thread(
            self.importer.import_release, files, album, "unverified", batch
        )
        year = str(edits["year"]) if edits["year"] else ""
        await self._finish(job, cand, results, batch, (album.artist, album.album, year))

    async def _finish(
        self,
        job: JobRecord,
        cand: Candidate,
        results: list[ImportResult],
        batch: str,
        recheck: AlbumKey | None = None,  # an unverified import: look for it on MusicBrainz later
    ) -> None:
        errors = [f"{r.src.name}: {r.error}" for r in results if r.error]
        if results and not errors:
            await self._release(cand, [r.src for r in results], batch)
        now = self.clock.now()
        with self.sessions.begin() as s:
            req = self._request(s, job)
            req.status = "failed" if errors else "imported"
            req.last_error = "; ".join(errors) or None
            if len(errors) < len(results):
                enqueue_rescan(s, now)
                if recheck:
                    enqueue_recheck(s, now, recheck, None)

    async def _unowned(
        self, album: AlbumInfo, files: list[canonical.File], batch: str
    ) -> list[canonical.File]:
        """Drop files whose recording the group already has. They stay in /downloads: release
        only deletes a download that has a verified import of its own."""
        with self.sessions() as s:
            have = canonical_db.owned(
                s, str(album.release_group_id), [t.recording_id or "" for _, t, _ in files], batch
            )
        if have:
            log.info(
                "skipping recordings already owned in %s: %s",
                album.release_id,
                sorted(map(str, have)),
            )
        return [f for f in files if f[1].recording_id not in have]

    async def _canonical_id(self, album: AlbumInfo) -> str | None:
        """The canonical release of the album's group: the stored choice, else the policy's pick,
        stored. Reading first means a retry never browses again."""
        group = album.release_group_id
        if not group:
            return None
        with self.sessions() as s:
            if stored := canonical_db.get(s, group):
                return stored
        releases = await self.metadata.browse_group_releases(group)
        if not releases:  # nothing to choose from: file this release as is, decide next time
            return None
        pick = canonical.choose(releases, self.canonical_media, self.canonical_countries)
        with self.sessions.begin() as s:
            return canonical_db.store(s, group, pick)

    async def _release(self, cand: Candidate, srcs: list[Path], batch: str) -> None:
        """Delete the verified downloads and clear slskd's list. Cleanup only: any failure is
        logged and the import still counts."""
        try:
            await asyncio.to_thread(self.importer.release, srcs, batch)
            await self.source.clear(cand)
        except Exception:
            log.warning("releasing downloads of %s skipped", batch, exc_info=True)

    async def _cover(self, album: AlbumInfo, results: list[ImportResult], batch: str) -> None:
        """cover.jpg in the album folder, never replacing one. Art is optional: any failure is
        logged and the import still counts."""
        rel = next((r.rel for r in results if r.rel), None)
        if rel is None or album.release_id is None:
            return
        cover = f"{rel.rpartition('/')[0]}/cover.jpg".removeprefix("/")
        try:
            if self.importer.has_file(cover):
                return
            if data := await self.coverart.front(album.release_id, album.release_group_id):
                await asyncio.to_thread(self.importer.write_file, cover, data, batch)
        except Exception:
            log.warning("cover art for %s skipped", album.release_id, exc_info=True)

    async def rescan(self, _job: JobRecord) -> None:
        await self.target.rescan()


def _aware(t: datetime, now: datetime) -> datetime:
    """SQLite hands back naive datetimes; they were stored in UTC."""
    return t if t.tzinfo else t.replace(tzinfo=now.tzinfo)


def _missing(cand: Candidate, local: tuple[str, ...]) -> str | None:
    if len(local) != len(cand.files):
        return "slskd reported no local paths"
    for f, path in zip(cand.files, local, strict=True):
        p = Path(path)
        if not p.is_file():
            return f"{p.name} is not in the downloads folder"
        if p.stat().st_size != f.size:
            return f"{p.name} is {p.stat().st_size} bytes, expected {f.size}"
    return None


def file_track(path: Path, duration: float, acoustid: tuple[Any, ...]) -> FileTrack:
    t = tags.read_fields(path)
    disc, track, title = parse_name(path.stem)
    return FileTrack(
        title=t.get("title") or title,
        artist=t.get("artist", ""),
        length=duration,
        track=_int(t.get("tracknumber")) or track,
        disc=_int(t.get("discnumber")) or disc,
        recording_id=t.get("musicbrainz_trackid"),
        acoustid=acoustid,
    )


def _int(v: str | None) -> int | None:
    return int(v) if v and v.isdigit() else None


def likelies(paths: list[Path]) -> Likelies:
    """The most common album-level tag across the files."""
    seen = [tags.read_fields(p) for p in paths]

    def common(key: str) -> str | None:
        vals = Counter(v for t in seen if (v := t.get(key)))
        return vals.most_common(1)[0][0] if vals else None

    year = common("date")
    return Likelies(
        artist=common("albumartist") or common("artist"),
        album=common("album"),
        year=_int(year[:4]) if year else None,
        release_id=common("musicbrainz_albumid"),
    )


def _best(f: FileTrack) -> dict[str, Any]:
    best = max(f.acoustid, key=lambda r: r.score, default=None)
    return {"score": best.score, "recording_ids": list(best.recording_ids)} if best else {}


def acoustid_id(f: FileTrack) -> str | None:
    best = max(f.acoustid, key=lambda r: r.score, default=None)
    return best.id if best and best.score >= ACOUSTID_ID_SCORE else None


def _source(cand: Candidate, local: list[Path] | None = None) -> dict[str, Any]:
    return {
        "peer": cand.username,
        "folder": cand.folder,
        "files": [
            {
                "name": f.path.rsplit("\\", 1)[-1],
                "format": f.extension,
                "bitrate": f.bitrate,
                "size": f.size,
                "duration": f.duration,
                "local_path": str(local[i]) if local else None,
            }
            for i, f in enumerate(cand.files)
        ],
    }


def _target(album: AlbumInfo) -> dict[str, Any]:
    return {
        "title": album.album,
        "artist": album.artist,
        "tracks": [
            {"position": t.index, "title": t.title, "length": t.length} for t in album.tracks
        ],
    }
