from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from mixsync.core.matching import AlbumInfo, Candidate, TransferInfo
from mixsync.core.types import (
    AcoustIdResult,
    EntityRef,
    Fingerprint,
    ReleaseGroupHit,
    ReleaseRef,
)


@dataclass(frozen=True)
class JournalOp:
    id: int
    batch_id: str
    kind: str  # copy / move / trash / release / retag / purge
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
    async def browse_group_releases(self, release_group_mbid: str) -> list[ReleaseRef]: ...


class ReleaseWatch(Protocol):
    """What the watchlist asks MusicBrainz; `kind` is "artist" or "label"."""

    async def new_release_groups(
        self, kind: str, mbid: str, since: str | None
    ) -> list[ReleaseGroupHit]: ...
    async def search_entities(self, kind: str, name: str) -> list[EntityRef]: ...
    async def entity_name(self, kind: str, mbid: str) -> str: ...
    async def artist_discography(self, mbid: str) -> tuple[list[ReleaseGroupHit], bool]: ...
    async def browse_group_releases(self, release_group_mbid: str) -> list[ReleaseRef]: ...


class DownloadSource(Protocol):
    async def search(self, query: str) -> list[Candidate]: ...
    async def enqueue(self, candidate: Candidate) -> None: ...
    async def status(self, candidate: Candidate) -> TransferInfo: ...
    async def active_downloads(self) -> dict[str, int]: ...  # transfer id -> bytes, moving now
    async def clear(self, candidate: Candidate) -> None: ...


Fingerprinter = Callable[[Path], Awaitable[Fingerprint]]


class AcoustIdLookup(Protocol):
    async def lookup(self, fp: Fingerprint) -> list[AcoustIdResult]: ...


class AcoustIdSubmit(Protocol):
    async def submit(self, items: list[tuple[Fingerprint, str]]) -> list[int]: ...

    async def submission_status(self, ids: list[int]) -> dict[int, str]: ...


class CoverArt(Protocol):
    async def front(self, release_mbid: str, release_group_mbid: str | None) -> bytes | None: ...


class LibraryTarget(Protocol):
    async def rescan(self) -> None: ...


class UserLibrary(Protocol):
    async def push_playlist(
        self, name: str, song_ids: list[str], existing_id: str | None
    ) -> str: ...
    async def starred_song_ids(self) -> set[str]: ...
    async def play_counts(
        self, song_ids: list[str], playlist_id: str | None = None
    ) -> dict[str, int]: ...
    async def music_folder_id(self, name: str) -> str | None: ...
    async def resolve(
        self, recording_mbid: str, title: str, artist: str, music_folder_id: str | None = None
    ) -> str | None: ...
