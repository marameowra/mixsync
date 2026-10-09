from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from mixsync.core.matching import AlbumInfo
from mixsync.core.types import ReleaseRef


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
    def fail(self, op_id: int, error: str) -> None: ...
    def pending(self) -> list[JournalOp]: ...


class MetadataProvider(Protocol):
    async def get_release(self, mbid: str) -> AlbumInfo: ...
    async def search_releases(
        self, artist: str, album: str, limit: int = 10
    ) -> list[ReleaseRef]: ...
