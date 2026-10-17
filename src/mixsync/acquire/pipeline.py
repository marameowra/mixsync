"""The acquire job chain: search -> download -> verify -> import (or review) -> rescan.

Every handler reads what it needs, does its I/O outside any transaction, then writes its result
and enqueues the next job in one transaction. The next job's idempotency key is derived from the
current job's id, so a handler re-run after a crash never forks the chain.
"""

import asyncio
import logging
import tempfile
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire import lifecycle, verify
from mixsync.acquire.evidence import Evidence
from mixsync.acquire.gate import DownloadGate
from mixsync.acquire.requests import (
    NO_RESULTS_BACKOFF,
    AlbumKey,
    candidate_dict,
    candidate_from_dict,
    enqueue_import,
    enqueue_recheck,
    enqueue_rescan,
    hold_imports,
    profile_for,
    retry_search,
    search_priority,
)
from mixsync.core.clock import Clock, aware
from mixsync.core.errors import AuthError, PermanentError
from mixsync.core.jobs import JobKind, JobState
from mixsync.core.matching import (
    AlbumInfo,
    Band,
    Candidate,
    FileTrack,
    Likelies,
    MatchResult,
    Profile,
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
from mixsync.db.models.discovery import ProvisionalTrack
from mixsync.db.models.library import Track
from mixsync.db.models.matching import MatchDecision
from mixsync.db.models.work import Job, Request
from mixsync.db.queue import JobRecord, enqueue_in
from mixsync.library import canonical, tags
from mixsync.library.encode import encodable, to_flac
from mixsync.library.importer import Importer, ImportResult
from mixsync.match.candidates import parse_name, present_tracks, rank
from mixsync.match.features import string_dist
from mixsync.match.scorer import (
    DEFAULT_WEIGHTS,
    SCORER_VERSION,
    assign_tracks,
    best_release,
    score_files,
)
from mixsync.match.vetoes import all_confident

log = logging.getLogger(__name__)

# A partial import needs the album tag to name the release: 0.2 forgives an edition suffix or a
# typo, not a compilation title.
PARTIAL_ALBUM_MAX_DIST = 0.2

Handler = Callable[[JobRecord], Awaitable[None]]

SEARCH_DEBOUNCE = timedelta(minutes=10)  # one request never searches twice within this
MAX_SEARCHES = 2
IDENTIFY_SEARCH, IDENTIFY_RELEASES = 5, 3  # a migrated album: search hits, releases scored
SEARCH_BUSY_DELAY = timedelta(minutes=1)
DOWNLOAD_BUSY_DELAY = timedelta(seconds=30)  # each look at a full cap is also a rate sample
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
    reencode: bool = False  # lossless files go into the library as FLAC (see library/encode.py)
    flac_level: int = 8
    gate: DownloadGate = field(default_factory=DownloadGate)

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
            if not lifecycle.can_search(req):
                return  # superseded: another job already moved this request on
            wait = req.next_search_at and aware(req.next_search_at)
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
                    priority=search_priority(req),
                    idempotency_key=f"search:defer:{job.id}",
                )
                return
            mbid, tried, profile = req.release_mbid, req.tried, profile_for(s, req.user_id)
            wanted = set(req.recording_mbids) if req.provisional else None

        album = await self.metadata.get_release(mbid)
        if not album.tracks:
            with self.sessions.begin() as s:
                req = self._request(s, job)
                req.artist, req.album = album.artist[:255], album.album[:255]
                lifecycle.failed(req, "release has no tracklist")
            return
        owned: set[str | None] = set()  # recordings of this album already in the library
        if wanted is not None:  # discovery: any copy of the wanted recordings will do
            with self.sessions() as s:
                have = self._have_recordings(s, wanted)
            if wanted <= have:
                with self.sessions.begin() as s:
                    req = self._request(s, job)
                    req.artist, req.album = album.artist[:255], album.album[:255]
                    req.release_group_mbid = req.release_group_mbid or album.release_group_id
                    lifecycle.owned(req)
                return
            if album.tracks and not wanted & {t.recording_id for t in album.tracks}:
                with self.sessions.begin() as s:
                    req = self._request(s, job)
                    req.artist, req.album = album.artist[:255], album.album[:255]
                    lifecycle.dead_end(req, "none of the wanted recordings are on this release")
                return
        elif album.release_group_id and album.tracks:
            with self.sessions() as s:
                have = canonical_db.owned(
                    s, album.release_group_id, [t.recording_id or "" for t in album.tracks]
                )
            owned = have
            if all(t.recording_id in have for t in album.tracks):  # nothing left to fetch
                with self.sessions.begin() as s:
                    req = self._request(s, job)
                    req.artist, req.album = album.artist[:255], album.album[:255]
                    req.release_group_mbid = req.release_group_mbid or album.release_group_id
                    lifecycle.owned(req)
                return
        ranked: list[tuple[Candidate, Any]] = []
        for q in self.queries(album.artist, album.album):
            found = [c for c in await self.source.search(q) if [c.username, c.folder] not in tried]
            ranked = [
                cr
                for cr in rank(found, album, profile)
                if cr[1].band is not Band.REJECT
                # a folder with nothing new would only download and import nothing
                and (
                    not owned
                    or any(t.recording_id not in owned for t in present_tracks(cr[0], album))
                )
            ]
            if ranked:
                break

        now = self.clock.now()
        with self.sessions.begin() as s:
            req = self._request(s, job)
            req.artist, req.album = album.artist[:255], album.album[:255]
            req.release_group_mbid = req.release_group_mbid or album.release_group_id
            if not ranked:
                delay = NO_RESULTS_BACKOFF[
                    min(req.no_results_attempts, len(NO_RESULTS_BACKOFF) - 1)
                ]
                req.no_results_attempts += 1
                lifecycle.no_results(req)
                req.next_search_at = now + delay
                enqueue_in(
                    s,
                    now,
                    JobKind.SEARCH,
                    job.payload,
                    run_after=now + delay,
                    priority=search_priority(req),
                    idempotency_key=f"search:again:{job.id}",
                )
                return
            cand, result = ranked[0]
            decisions.record(
                s,
                result,
                evidence=Evidence.searched(req.id, cand, album).to_json(),
                user_id=req.user_id,
                stage=1,
                release_mbid=mbid,
            )
            lifecycle.downloading(req)
            req.next_search_at = now + SEARCH_DEBOUNCE
            enqueue_in(
                s,
                now,
                JobKind.DOWNLOAD,
                {"request_id": req.id, "candidate": candidate_dict(cand)},
                idempotency_key=f"download:{job.id}",
            )

    @staticmethod
    def _have_recordings(s: Session, wanted: set[str], batch: str = "") -> set[str | None]:
        """Which wanted recordings the library or an active discovery track (not of this
        batch: a resumed import must still finish its own) already has."""
        return set(
            s.scalars(select(Track.recording_mbid).where(Track.recording_mbid.in_(wanted)))
        ) | set(
            s.scalars(
                select(ProvisionalTrack.recording_mbid).where(
                    ProvisionalTrack.recording_mbid.in_(wanted),
                    ProvisionalTrack.status == "active",
                    ProvisionalTrack.batch_id != batch,
                )
            )
        )

    async def download(self, job: JobRecord) -> None:
        """First run enqueues the transfer; then each run is one status check that schedules
        the next as a fresh job, so a slow transfer never uses up the retry budget (SK-P05)."""
        p = job.payload
        cand = candidate_from_dict(p["candidate"])
        now = self.clock.now()
        if "bytes" not in p and self.gate.full(now, await self.source.active_downloads()):
            with self.sessions.begin() as s:  # a fresh job, like a busy search: no attempt burnt
                enqueue_in(
                    s,
                    now,
                    JobKind.DOWNLOAD,
                    p,
                    run_after=now + DOWNLOAD_BUSY_DELAY,
                    idempotency_key=f"download:defer:{job.id}",
                )
            return
        try:
            if "bytes" not in p:
                await self.source.enqueue(cand)
                self._monitor(job, now, 0, now)
                return
            info = await self.source.status(cand)
        except AuthError:
            raise
        except PermanentError as e:  # this peer is out, not the request: try the next candidate
            with self.sessions.begin() as s:
                retry_search(s, now, self._request(s, job), cand.username, cand.folder, str(e))
            return
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
            verify.start(
                s,
                now,
                req,
                req.release_mbid,
                p["candidate"],
                list(info.local_paths),
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
        """AcoustID is always consulted: if it is unreachable the job retries, never imports.
        A migrated album has no release yet: `_identify` picks one."""
        p = job.payload
        with self.sessions() as s:
            if _stale(s, job, self._request(s, job)):  # before spending any rate-limited calls
                return
        cand = candidate_from_dict(p["candidate"])
        paths = [Path(x) for x in p["paths"]]
        album: AlbumInfo | None = None
        if p["release_mbid"]:  # before any AcoustID call: a missing release costs none
            try:
                album = await self.metadata.get_release(p["release_mbid"])
            except PermanentError:  # a repick named a release MusicBrainz does not have
                with self.sessions.begin() as s:
                    if not _stale(s, job, self._request(s, job)):
                        self._reopen(s, job, p["release_mbid"])
                return
        for path in paths:  # import must be able to turn WAV/AIFF into FLAC, so check now
            if why := await self._unusable(path):
                with self.sessions.begin() as s:
                    if not _stale(s, job, self._request(s, job)):
                        retry_search(
                            s,
                            self.clock.now(),
                            self._request(s, job),
                            cand.username,
                            cand.folder,
                            why,
                        )
                return
        files: list[FileTrack] = []
        for path in paths:
            fp = await self.fingerprint(path)
            results = await self.acoustid.lookup(fp)
            files.append(file_track(path, fp.duration, tuple(results)))
        like = likelies(paths)
        with self.sessions() as s:
            req = self._request(s, job)
            profile, migrating = profile_for(s, req.user_id), req.from_files
            provisional = req.provisional
        if album is not None:
            result = score_files(files, like, album, profile)
        else:
            album, result = await self._identify(files, like, profile, cand.folder)
        if migrating and result.band is Band.REJECT:  # nothing to search again: a human decides
            result = replace(result, band=Band.REVIEW)
        pairs, extra, _ = assign_tracks(files, album.tracks)
        index = {id(f): i for i, f in enumerate(files)}
        track_no = {id(t): i for i, t in enumerate(album.tracks)}
        assignment = [
            {
                "local_path": str(paths[index[id(f)]]),
                "track": track_no[id(t)],  # old decisions have only this
                "recording_id": t.recording_id,
                "acoustid_id": acoustid_id(f),
            }
            for f, t in pairs
        ]
        unassigned = [str(paths[index[id(f)]]) for f in extra]  # import copies only the paired
        if migrating and unassigned and result.band is Band.AUTO_ACCEPT:  # never drop silently
            result = replace(result, band=Band.REVIEW)
        partial = ""
        if pairs and len(pairs) < len(album.tracks) and result.band is not Band.AUTO_ACCEPT:
            # deliberately partial: missing tracks cost nothing, but only when AcoustID is sure
            # of every file; otherwise the normal bands (and a retry) apply
            free = score_files(
                files, like, album, profile, {**DEFAULT_WEIGHTS, "missing_tracks": 0.0}
            )
            if (
                free.band is Band.AUTO_ACCEPT
                and all_confident(pairs)
                and string_dist(like.album, album.album) <= PARTIAL_ALBUM_MAX_DIST
            ):
                result, partial = free, f"{len(pairs)} of {len(album.tracks)} tracks present"
        evidence = Evidence.verified(
            p["request_id"],
            p["candidate"],
            cand,
            paths,
            album,
            unassigned=unassigned,
            partial=partial,
            acoustid=[_best(f) for f in files],
            duration_deltas=[abs((f.length or 0) - (t.length or 0)) for f, t in pairs],
            assignment=assignment,
        ).to_json()

        now = self.clock.now()
        with self.sessions.begin() as s:
            req = self._request(s, job)
            if _stale(s, job, req):
                return
            if migrating:
                req.release_mbid = p["release_mbid"] or album.release_id or ""
                req.artist = album.artist[:255] or req.artist  # never blank a name from the tags
                req.album = album.album[:255] or req.album
            d = decisions.record(
                s,
                result,
                evidence=evidence,
                user_id=req.user_id,
                stage=2,
                release_mbid=p["release_mbid"] or album.release_id or "",
            )
            if result.band is Band.AUTO_ACCEPT:
                enqueue_import(s, now, req, d.id)
            elif result.band is Band.REVIEW and provisional:  # discovery never fills Review
                d.status = "rejected_auto"
                why = "; ".join(result.vetoes) or f"distance {result.distance:.2f}"
                retry_search(
                    s,
                    now,
                    req,
                    cand.username,
                    cand.folder,
                    f"would need review, which discovery skips: {why}",
                )
            elif result.band is Band.REVIEW:
                lifecycle.review(req)
            else:  # files stay in /downloads: reject never deletes
                retry_search(s, now, req, cand.username, cand.folder, "rejected after download")

    async def _unusable(self, path: Path) -> str | None:
        """Why this download cannot be imported (naming the file), else None. Only WAV/AIFF
        need the ffprobe check; other readable formats import as they are."""
        if not await asyncio.to_thread(tags.readable, path):
            return f"unsupported audio: {path.name} is not readable audio"
        if await asyncio.to_thread(tags.is_pcm, path) and (why := await encodable(path)):
            return f"unsupported audio: {path.name}: {why}"
        return None

    def _reopen(self, s: Session, job: JobRecord, mbid: str) -> None:
        """Put the review item whose repick named `mbid` back, so the reviewer can choose again."""
        req = self._request(s, job)
        for d in s.scalars(
            select(MatchDecision)
            .where(MatchDecision.chosen_release_mbid == mbid, MatchDecision.status == "repick")
            .order_by(MatchDecision.id.desc())
        ):
            if Evidence.parse(d.evidence).request_id == req.id:
                decisions.reopen(s, d.id)
                lifecycle.review(req, f"release {mbid} not found on MusicBrainz")
                return
        raise PermanentError(f"release {mbid} not found on MusicBrainz")

    async def _identify(
        self, files: list[FileTrack], like: Likelies, profile: Profile, folder: str
    ) -> tuple[AlbumInfo, MatchResult]:
        """The best-scoring release for files of unknown release: the one their tags name, else
        the top MusicBrainz search hits. No candidate gives an empty album and a review band."""
        albums: list[AlbumInfo] = []
        if tagged := _uuid(like.release_id):
            try:
                albums = [await self.metadata.get_release(tagged)]
            except PermanentError:  # a stale or unknown id: search instead (transient errors retry)
                log.info("tagged release %s is not on MusicBrainz", tagged)
        title = like.album or Path(folder).name  # untagged files: the folder name is all we have
        if not albums and (like.artist or title):
            refs = await self.metadata.search_releases(
                like.artist or "", title, limit=IDENTIFY_SEARCH
            )
            albums = [
                await self.metadata.get_release(r.release_id) for r in refs[:IDENTIFY_RELEASES]
            ]
        if not albums:
            none = MatchResult(
                1.0, Band.REVIEW, (), ("no release found on MusicBrainz",), SCORER_VERSION
            )
            return AlbumInfo(like.artist or "", like.album or "", ()), none
        return best_release(files, like, albums, profile)

    async def import_(self, job: JobRecord) -> None:
        rid, did = job.payload["request_id"], job.payload["decision_id"]
        with self.sessions() as s:
            d = s.get(MatchDecision, did)
            if d is None:
                raise LookupError(f"decision {did} does not exist")
            evidence = Evidence.parse(d.evidence)
            cand = candidate_from_dict(evidence.candidate)
            edits = evidence.unverified
            mbid = d.chosen_release_mbid or d.release_mbid
            assignment = evidence.assignment
            # an earlier run made us canonical
            switched = evidence.canonical_switched_from is not None
            req = self._request(s, job)
            wanted = set(req.recording_mbids) if req.provisional else None
        if edits:
            return await self._import_unverified(job, cand, edits)
        if not mbid:
            raise PermanentError(
                "no release was found for these files; pick one or mark unverified"
            )
        if not assignment:  # never mark a request imported when nothing was assigned
            raise PermanentError("the decision assigns no files to import")
        album = await self.metadata.get_release(mbid)
        # Same batch id on every retry, so a resumed import skips what is already in.
        batch = verify.batch_id(rid, did)
        files: list[tuple[Path, TrackInfo, str | None]] = []
        for path, track, acoustid in evidence.resolve(album):
            if track is None:  # the release changed since verify
                if any(self._imported(Path(x["local_path"]), batch) for x in assignment):
                    raise PermanentError(
                        "the release changed on MusicBrainz after part of this album was "
                        "imported; check it by hand"
                    )
                with self.sessions.begin() as s:
                    verify.again(s, self.clock.now(), self._request(s, job), mbid, evidence)
                return
            files.append((path, track, acoustid))
        if wanted is not None:
            return await self._import_provisional(job, cand, album, files, wanted, batch)
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
                    row = s.get(MatchDecision, did)
                    if row:
                        row.evidence = (
                            Evidence.parse(row.evidence).with_switched_from(cid).to_json()
                        )
                    switched = True
                    break
            # lost a race to another switch: decide again against the stored choice
        if album.release_group_id and target is album and not switched:  # else already filtered
            # shortcut: two workers importing the same release at once can still duplicate (rows
            # not yet committed); the fragmentation merge cleans that up.
            files = await self._unowned(album, files, batch)
        if n and not files:  # every file is a verified duplicate: nothing to import or rescan
            with self.sessions() as s:
                have = canonical_db.owned(
                    s, str(album.release_group_id), [t.recording_id or "" for t in album.tracks]
                )
            left = [t for t in album.tracks if t.recording_id not in have]
            with self.sessions.begin() as s:
                req = self._request(s, job)
                if left and not req.from_files and album.release_group_id:
                    # another copy of what we have: the rest of the album is still wanted
                    text = f"{len(album.tracks) - len(left)} of {len(album.tracks)} tracks present"
                    self._keep_looking(s, self.clock.now(), req, cand, text)
                else:
                    lifecycle.owned(req)
            return
        results = await self._import_files(files, target, "verified", batch)
        await self._cover(target, results, batch)
        await self._finish(
            job, cand, results, batch, unassigned=evidence.unassigned, partial=evidence.partial
        )

    async def _import_provisional(
        self,
        job: JobRecord,
        cand: Candidate,
        album: AlbumInfo,
        files: list[tuple[Path, TrackInfo, str | None]],
        wanted: set[str],
        batch: str,
    ) -> None:
        """Only the wanted recordings, under /data/discover: no canonical logic, no `tracks`
        rows, no cover. The rest of the album stays in /downloads."""
        on_release = {t.recording_id for t in album.tracks} & wanted
        with self.sessions() as s:
            todo = wanted - self._have_recordings(s, wanted, batch)  # also covers a racing user
        seen: set[str | None] = set()
        picked: list[tuple[Path, TrackInfo, str | None]] = []
        for f in files:  # the first file of a recording that is on the release twice
            rec = f[1].recording_id
            # a copy this batch already made is recorded even if the recording is owned by now
            if (
                rec in wanted
                and rec not in seen
                and (rec in todo or self._imported(f[0], batch, "discover"))
            ):
                seen.add(f[1].recording_id)
                picked.append(f)
        files = picked
        note = (
            f"{len(wanted) - len(on_release)} of {len(wanted)} wanted recordings "
            "not on this release"
            if on_release != wanted
            else ""
        )
        if not files:
            with self.sessions.begin() as s:
                req = self._request(s, job)
                if todo:
                    lifecycle.dead_end(
                        req, "none of the wanted recordings are on the downloaded release"
                    )
                else:  # everything wanted is owned by now
                    lifecycle.owned(req)
            return
        results = await self._import_files(files, album, "verified", batch, "discover")
        by_src = {f[0]: f for f in files}
        with self.sessions.begin() as s:  # idempotent: a resumed import adds no second row
            for r in results:
                if r.rel is None or s.scalar(
                    select(ProvisionalTrack.id).where(
                        ProvisionalTrack.path == r.rel, ProvisionalTrack.batch_id == batch
                    )
                ):
                    continue
                _, track, acoustid = by_src[r.src]
                s.add(
                    ProvisionalTrack(
                        path=r.rel,
                        recording_mbid=track.recording_id,
                        release_mbid=album.release_id,
                        release_group_mbid=album.release_group_id,
                        acoustid_id=acoustid,
                        batch_id=batch,
                        imported_at=self.clock.now(),
                        title=track.title,
                        artist=track.artist or album.artist,
                    )
                )
        await self._finish(job, cand, results, batch, root="discover", note=note)

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
        batch = verify.batch_id(job.payload["request_id"], job.payload["decision_id"])
        results = await self._import_files(files, album, "unverified", batch)
        year = str(edits["year"]) if edits["year"] else ""
        await self._finish(job, cand, results, batch, (album.artist, album.album, year))

    async def _import_files(
        self,
        files: list[tuple[Path, TrackInfo, str | None]],
        album: AlbumInfo,
        status: str,
        batch: str,
        root: str = "library",
    ) -> list[ImportResult]:
        """Lossless files are re-encoded to FLAC in a work dir first (the downloads are never
        touched); the encodes run as subprocesses so the job's heartbeat keeps going."""
        if not self.reencode:
            return await asyncio.to_thread(
                self.importer.import_release, files, album, status, batch, None, root
            )
        batch_dir = self.importer.fileops.work / batch
        batch_dir.mkdir(parents=True, exist_ok=True)
        # Its own dir per run: an overlapping run of the same job must not rewrite our files.
        work = Path(tempfile.mkdtemp(dir=batch_dir))
        try:
            encoded: dict[Path, Path] = {}
            for i, (src, _, _) in enumerate(files):
                if await asyncio.to_thread(self._imported, src, batch, root):
                    continue  # a resumed batch: this file is already in
                try:
                    out = await to_flac(src, work / str(i), self.flac_level)
                except (PermanentError, RuntimeError) as e:  # import this one as it is
                    log.warning("not re-encoding %s: %s", src, e)
                    continue
                if out != src:
                    encoded[src] = out
            return await asyncio.to_thread(
                self.importer.import_release, files, album, status, batch, encoded, root
            )
        finally:
            await asyncio.to_thread(self.importer.fileops.clear_work, f"{batch}/{work.name}")

    async def _finish(
        self,
        job: JobRecord,
        cand: Candidate,
        results: list[ImportResult],
        batch: str,
        recheck: AlbumKey | None = None,  # an unverified import: look for it on MusicBrainz later
        unassigned: tuple[str, ...] = (),  # downloaded files the release has no track for
        root: str = "library",
        note: str = "",
        partial: str = "",  # "N of M tracks present": import what is here, keep looking
    ) -> None:
        errors = [f"{r.src.name}: {r.error}" for r in results if r.error]
        with self.sessions() as s:
            req = self._request(s, job)
            migrating, local = req.migration_id is not None, req.from_files
        if results and not errors and not migrating:  # the old library is never touched
            await self._release(cand, [r.src for r in results], batch, root)
        now = self.clock.now()
        with self.sessions.begin() as s:
            req = self._request(s, job)
            notes = [*errors, *([note] if note else [])]
            if unassigned:
                names = ", ".join(Path(u).name for u in unassigned)
                notes.append(f"{len(unassigned)} file(s) not on the release: {names}")
            lifecycle.finished(
                req, errors, notes, unverified=bool(local and recheck), dead_end=bool(note)
            )
            if partial and not errors and not local:
                self._keep_looking(s, now, req, cand, partial, notes)
            if len(errors) < len(results):
                enqueue_rescan(s, now)
                if root == "discover":
                    hold_imports(s, now, batch)
                if recheck:
                    enqueue_recheck(s, now, recheck, None)

    @staticmethod
    def _keep_looking(
        s: Session,
        now: datetime,
        req: Request,
        cand: Candidate,
        partial: str,
        notes: list[str] | None = None,
    ) -> None:
        """Open until the rest of the album is here. The next search skips owned recordings and
        the import drops them, so nothing already imported is touched."""
        delay = NO_RESULTS_BACKOFF[min(req.no_results_attempts, len(NO_RESULTS_BACKOFF) - 1)]
        lifecycle.keep_looking(req, partial, notes or [])
        req.tried = [*req.tried, [cand.username, cand.folder]]
        req.no_results_attempts += 1
        req.next_search_at = now + delay
        enqueue_in(
            s,
            now,
            JobKind.SEARCH,
            {"request_id": req.id},
            run_after=now + delay,
            priority=search_priority(req),
        )

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

    def _imported(self, src: Path, batch: str, root: str = "library") -> bool:
        return self.importer.fileops.find_imported(batch, src, root) is not None

    async def _release(
        self, cand: Candidate, srcs: list[Path], batch: str, root: str = "library"
    ) -> None:
        """Delete the verified downloads and clear slskd's list. Cleanup only: any failure is
        logged and the import still counts."""
        try:
            for src in srcs:
                await asyncio.to_thread(self.importer.fileops.release, src, batch, root)
            if cand.username:  # a dropped album never came from a peer
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
            if self.importer.fileops.has(cover):
                return
            if data := await self.coverart.front(album.release_id, album.release_group_id):
                await asyncio.to_thread(self.importer.fileops.write_new, cover, data, batch)
        except Exception:
            log.warning("cover art for %s skipped", album.release_id, exc_info=True)

    async def rescan(self, _job: JobRecord) -> None:
        await self.target.rescan()


def _stale(s: Session, job: JobRecord, req: Request) -> bool:
    """A re-run of a VERIFY that already recorded its decision, or one a repick superseded."""
    return req.status != "verifying" or verify.superseded(s, job.id, req.id)


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


def _uuid(v: str | None) -> str | None:
    try:
        return str(uuid.UUID(v)) if v else None
    except ValueError:
        return None  # not an id: a stray tag must not reach the API


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
