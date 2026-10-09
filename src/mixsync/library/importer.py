from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import Clock
from mixsync.core.matching import AlbumInfo, TrackInfo
from mixsync.db.models.library import Track
from mixsync.db.models.safety import TagSnapshot
from mixsync.library import paths, tags
from mixsync.library.fileops import FileOps


@dataclass(frozen=True)
class ImportResult:
    src: Path
    rel: str | None  # library-relative path; None if the file failed
    error: str | None = None


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
    ) -> list[ImportResult]:
        """Each file is its own journaled op: one failure leaves the others imported. The
        sources are not released here."""
        return [self._one(f, album, status, batch_id) for f in files]

    def _one(
        self,
        file: tuple[Path, TrackInfo, str | None],
        album: AlbumInfo,
        status: str,
        batch_id: str,
    ) -> ImportResult:
        src, track, acoustid_id = file
        tagset = tags.TagSet(
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
        )
        try:
            if done := self._fileops.find_imported(batch_id, src):
                self._ensure_row(done, tagset, acoustid_id, status, batch_id)
                return ImportResult(src, done)
            rel = paths.unique(
                paths.render(
                    self._template,
                    albumartist=tagset.albumartist,
                    album=tagset.album,
                    year=tagset.year,
                    disc=tagset.disc,
                    track=tagset.track,
                    title=tagset.title,
                    ext=src.suffix,
                ),
                lambda r: (self._fileops.library / r).exists(),
            )

            def write_tags(staging: Path, op_id: int) -> None:
                with self._sessions.begin() as s:  # committed before the write touches the file
                    s.add(
                        TagSnapshot(
                            file_path=rel,
                            op_id=op_id,
                            tags=tags.read_all(staging),
                            taken_at=self._clock.now(),
                        )
                    )
                tags.write(staging, tagset)

            self._fileops.import_file(src, rel, write_tags, batch_id)
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
