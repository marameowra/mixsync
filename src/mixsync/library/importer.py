from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import Clock
from mixsync.core.matching import AlbumInfo, TrackInfo
from mixsync.db.models.library import Track
from mixsync.db.models.safety import TagSnapshot
from mixsync.library import paths, tags
from mixsync.library.fileops import FileOps, WriteTags


@dataclass(frozen=True)
class ImportResult:
    src: Path
    rel: str | None  # library-relative path; None if the file failed
    error: str | None = None


def tagset_for(
    track: TrackInfo, album: AlbumInfo, acoustid_id: str | None, status: str
) -> tags.TagSet:
    return tags.TagSet(
        title=track.title,
        artist=track.artist or album.artist,
        album=album.album,
        albumartist=album.artist,
        track=track.medium_index or track.index or 1,
        disc=track.medium or 1,
        year=album.year,
        recording_id=track.recording_id,
        release_id=album.release_id,
        release_group_id=album.release_group_id,
        artist_ids=track.artist_ids,
        albumartist_ids=album.artist_ids,
        acoustid_id=acoustid_id,
        status=status,
        genres=album.genres,
    )


def render_path(template: str, t: tags.TagSet, ext: str) -> str:
    return paths.render(
        template,
        albumartist=t.albumartist,
        album=t.album,
        year=t.year,
        disc=t.disc,
        track=t.track,
        title=t.title,
        ext=ext,
        genre=t.genres[0] if t.genres else "Unknown Genre",
    )


def snapshotting_writer(
    sessions: sessionmaker[Session], clock: Clock, rel: str, tagset: tags.TagSet
) -> WriteTags:
    def write_tags(staging: Path, op_id: int) -> None:
        with sessions.begin() as s:  # committed before the write touches the file
            s.add(
                TagSnapshot(
                    file_path=rel, op_id=op_id, tags=tags.read_all(staging), taken_at=clock.now()
                )
            )
        tags.write(staging, tagset)

    return write_tags


class Importer:
    def __init__(
        self, fileops: FileOps, sessions: sessionmaker[Session], clock: Clock, template: str
    ) -> None:
        self._fileops = fileops
        self._sessions = sessions
        self._clock = clock
        self._template = template

    def import_release(
        self,
        files: list[tuple[Path, TrackInfo, str | None]],  # (source, track, acoustid_id)
        album: AlbumInfo,
        status: str,
        batch_id: str,
        encoded: dict[Path, Path] | None = None,  # source -> the file to copy in its place
        root: str = "library",  # "discover": no `tracks` rows, the caller records its own
    ) -> list[ImportResult]:
        """Each file is its own journaled op: one failure leaves the others imported. The
        sources are not released here."""
        enc = encoded or {}
        return [self._one(f, album, status, batch_id, enc.get(f[0]), root) for f in files]

    def release(self, srcs: list[Path], batch_id: str, root: str = "library") -> None:
        for src in srcs:
            self._fileops.release(src, batch_id, root)

    @property
    def work_dir(self) -> Path:
        return self._fileops.work

    def is_imported(self, src: Path, batch_id: str, root: str = "library") -> bool:
        return self._fileops.find_imported(batch_id, src, root) is not None

    def clear_work(self, batch_id: str) -> None:
        self._fileops.clear_work(batch_id)

    def has_file(self, rel: str) -> bool:
        return self._fileops.has(rel)

    def write_file(self, rel: str, data: bytes, batch_id: str) -> None:
        self._fileops.write_new(rel, data, batch_id)

    def _one(
        self,
        file: tuple[Path, TrackInfo, str | None],
        album: AlbumInfo,
        status: str,
        batch_id: str,
        encoded: Path | None,
        root: str,
    ) -> ImportResult:
        src, track, acoustid_id = file
        tagset = tagset_for(track, album, acoustid_id, status)
        try:
            main = root == "library"
            if done := self._fileops.find_imported(batch_id, src, root):
                if main:
                    self._ensure_row(done, tagset, acoustid_id, status, batch_id)
                return ImportResult(src, done)
            rel = paths.unique(
                render_path(self._template, tagset, (encoded or src).suffix),
                lambda r: (self._fileops.roots[root] / r).exists(),
            )
            write_tags = snapshotting_writer(self._sessions, self._clock, rel, tagset)
            self._fileops.import_file(encoded or src, rel, write_tags, batch_id, src, root)
            if main:
                self._ensure_row(rel, tagset, acoustid_id, status, batch_id)
        except Exception as e:
            return ImportResult(src, None, repr(e))
        return ImportResult(src, rel)

    def _ensure_row(
        self, rel: str, t: tags.TagSet, acoustid_id: str | None, status: str, batch_id: str
    ) -> None:
        with self._sessions.begin() as s:
            if s.scalar(select(Track.id).where(Track.path == rel)) is None:
                s.add(
                    Track(
                        path=rel,
                        recording_mbid=t.recording_id,
                        release_mbid=t.release_id,
                        release_group_mbid=t.release_group_id,
                        artist_mbids=list(t.artist_ids),
                        acoustid_id=acoustid_id,
                        status=status,
                        batch_id=batch_id,
                        imported_at=self._clock.now(),
                    )
                )
