from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from mixsync.core.matching import AlbumInfo, Candidate, TransferInfo
from mixsync.core.types import AcoustIdResult, Fingerprint, ReleaseRef


@dataclass(frozen=True)
class JournalOp:
    id: int
    batch_id: str
    kind: str  # copy / move / trash / retag / purge
    src: str
    dst: str
    src_hash: str | None
    dst_hash: str | None
    status: str  # started / done / failed / rolled_back
    started_at: datetime
    finished_at: datetime | None
    error: str | None


class Journal(Protocol):
    def begin(self, batch_id: str, kind: str, src: str, dst: str) -> int: ...
    def complete(self, op_id: int, dst_hash: str) -> None: ...
    def set_dst_hash(self, op_id: int, dst_hash: str) -> None: ...
    def fail(self, op_id: int, error: str) -> None: ...
    def rolled_back(self, op_id: int) -> None: ...
    def pending(self) -> list[JournalOp]: ...
    def find_completed(self, batch_id: str, src: str) -> JournalOp | None: ...


class MetadataProvider(Protocol):
    async def get_release(self, mbid: str) -> AlbumInfo: ...
    async def search_releases(
        self, artist: str, album: str, limit: int = 10
    ) -> list[ReleaseRef]: ...


class DownloadSource(Protocol):
    async def search(self, query: str) -> list[Candidate]: ...
    async def enqueue(self, candidate: Candidate) -> None: ...
    async def status(self, candidate: Candidate) -> TransferInfo: ...


class Fingerprinter(Protocol):
    async def fingerprint(self, path: Path) -> Fingerprint: ...


class AcoustIdLookup(Protocol):
    async def lookup(self, fp: Fingerprint) -> list[AcoustIdResult]: ...


class LibraryTarget(Protocol):
    async def rescan(self) -> None: ...
