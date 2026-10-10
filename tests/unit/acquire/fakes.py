"""Fakes of the core Protocols for the acquire pipeline tests. Plain imports only: the crash
test's subprocess loads this file by path."""

import shutil
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.pipeline import Pipeline
from mixsync.core.clock import Clock
from mixsync.core.errors import TransientError
from mixsync.core.matching import (
    AlbumInfo,
    Candidate,
    CandidateFile,
    TrackInfo,
    TransferInfo,
    TransferStatus,
)
from mixsync.core.types import AcoustIdResult, Fingerprint, ReleaseRef
from mixsync.db.journal import SqlJournal
from mixsync.library import tags
from mixsync.library.fileops import FileOps
from mixsync.library.importer import Importer
from mixsync.sources.query import queries

AUDIO = Path(__file__).parents[2] / "fixtures" / "audio"


class FixedClock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def now(self) -> datetime:
        return self.t

    def advance(self, d: timedelta) -> None:
        self.t += d


def make_album(prefix: str, release_id: str, titles: tuple[str, str]) -> AlbumInfo:
    tracks = tuple(
        TrackInfo(
            title,
            length=1.0,
            index=i,
            medium_index=i,
            medium=1,
            recording_id=f"{prefix}-rec-{i}",
            artist_ids=(f"{prefix}-artist",),
        )
        for i, title in enumerate(titles, 1)
    )
    return AlbumInfo(
        artist=f"{prefix} Band",
        album=f"{prefix} Album",
        tracks=tracks,
        year=2020,
        release_id=release_id,
        release_group_id=f"{prefix}-rg",
        artist_ids=(f"{prefix}-artist",),
    )


ALBUM = make_album("Alpha", "11111111-1111-1111-1111-111111111111", ("Song One", "Song Two"))
# Same tracks, another release of the group: what a reviewer repicks to.
ALBUM_B = replace(ALBUM, release_id="22222222-2222-2222-2222-222222222222", year=2021)
OTHER = make_album("Beta", "33333333-3333-3333-3333-333333333333", ("Tune One", "Tune Two"))
RECORDINGS = {t.title: t.recording_id or "" for a in (ALBUM, OTHER) for t in a.tracks}
CATALOG = {a.release_id or "": a for a in (ALBUM, ALBUM_B, OTHER)}


class FakeMetadata:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_release(self, mbid: str) -> AlbumInfo:
        self.calls.append(mbid)
        return CATALOG[mbid]

    async def search_releases(self, artist: str, album: str, limit: int = 10) -> list[ReleaseRef]:
        return []


class FakeSource:
    """Offers prepared folders; `enqueue` puts their (already tagged) files in `downloads`."""

    def __init__(self, downloads: Path, scratch: Path) -> None:
        self.downloads, self.scratch = downloads, scratch
        self.catalog: dict[str, list[Candidate]] = {}  # lowercase album title -> offers
        self.blobs: dict[tuple[str, str], dict[str, bytes]] = {}
        self.queries: list[str] = []
        self.enqueued: list[str] = []
        self.state: dict[str, TransferStatus] = {}  # by peer; default DONE
        self.write_nothing = False  # DONE, but no file on disk

    def add(
        self,
        username: str,
        folder: str,
        album: AlbumInfo = ALBUM,
        ext: str = "mp3",
        *,
        size_delta: int = 0,
    ) -> Candidate:
        """Files carry the usual tags (no MBIDs) so that stage 2 can auto-accept them. The
        offered sizes are those of the real copies (SK-P04), unless `size_delta` lies."""
        blobs: dict[str, bytes] = {}
        files: list[CandidateFile] = []
        for t in album.tracks:
            name = f"{t.index:02} {t.title}.{ext}"
            path = self.scratch / username / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(AUDIO / f"cc0-tone-01.{ext}", path)
            tags.write(
                path,
                tags.TagSet(
                    t.title, album.artist, album.album, album.artist, t.index or 1, 1, album.year
                ),
            )
            blobs[name] = path.read_bytes()
            size = len(blobs[name])
            files.append(
                CandidateFile(
                    f"{folder}\\{name}",
                    size + size_delta,
                    ext,
                    bitrate=None if ext == "flac" else round(size * 8 / 1000),
                    duration=1.0,
                )
            )
        cand = Candidate(username, folder, tuple(files), upload_speed=1_000_000)
        self.blobs[(username, folder)] = blobs
        self.catalog.setdefault(album.album.lower(), []).append(cand)
        return cand

    def _dir(self, c: Candidate) -> Path:
        return self.downloads / c.folder.rsplit("\\", 1)[-1]

    async def search(self, query: str) -> list[Candidate]:
        self.queries.append(query)
        return [c for k, v in self.catalog.items() if k in query.lower() for c in v]

    async def enqueue(self, candidate: Candidate) -> None:
        self.enqueued.append(candidate.username)
        d = self._dir(candidate)
        d.mkdir(parents=True, exist_ok=True)
        if not self.write_nothing:
            for name, data in self.blobs[(candidate.username, candidate.folder)].items():
                (d / name).write_bytes(data)

    async def status(self, candidate: Candidate) -> TransferInfo:
        state = self.state.get(candidate.username, TransferStatus.DONE)
        if state is not TransferStatus.DONE:
            return TransferInfo(state, 0)
        names = [f.path.rsplit("\\", 1)[-1] for f in candidate.files]
        return TransferInfo(state, 1, tuple(str(self._dir(candidate) / n) for n in names))


async def fake_fingerprint(path: Path) -> Fingerprint:
    return Fingerprint(1.0, f"{path.parent.name}/{path.name}")


class FakeAcoustId:
    """By default every file is the recording its name says, with a confident score."""

    def __init__(self) -> None:
        self.override: dict[str, list[AcoustIdResult]] = {}  # by "<folder>/<file name>"
        self.fail = 0  # this many lookups raise TransientError first

    async def lookup(self, fp: Fingerprint) -> list[AcoustIdResult]:
        if self.fail:
            self.fail -= 1
            raise TransientError("acoustid is down")
        if fp.fingerprint in self.override:
            return self.override[fp.fingerprint]
        title = fp.fingerprint.rpartition("/")[2].partition(" ")[2].rpartition(".")[0]
        return [AcoustIdResult(f"aid-{title}", 0.97, (RECORDINGS[title],))]


class FakeTarget:
    def __init__(self) -> None:
        self.rescans = 0

    async def rescan(self) -> None:
        self.rescans += 1


def make_fileops(sessions: sessionmaker[Session], clock: Clock, data_dir: Path) -> FileOps:
    return FileOps(data_dir, SqlJournal(sessions, clock), clock)


def make_pipeline(
    sessions: sessionmaker[Session],
    clock: Clock,
    ops: FileOps,
    template: str,
    source: FakeSource,
    acoustid: FakeAcoustId,
    metadata: FakeMetadata,
    target: FakeTarget,
) -> Pipeline:
    return Pipeline(
        sessions=sessions,
        clock=clock,
        source=source,
        queries=queries,
        metadata=metadata,
        fingerprint=fake_fingerprint,
        acoustid=acoustid,
        importer=Importer(ops, sessions, clock, template),
        target=target,
    )
