"""The one reader and builder of a Decision's stored evidence JSON (ADR 0005: the stored shape
stays complete and visible in the review UI; only the reading lives here)."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mixsync.core.matching import AlbumInfo, Candidate, TrackInfo


@dataclass(frozen=True)
class Evidence:
    raw: dict[str, Any]  # the stored JSON, unknown keys kept so a rewrite loses nothing

    @classmethod
    def parse(cls, stored: dict[str, Any]) -> "Evidence":
        return cls(stored)

    @classmethod
    def searched(cls, request_id: int, cand: Candidate, album: AlbumInfo) -> "Evidence":
        """Stage 1: the folder picked for download."""
        return cls({"request_id": request_id, "source": _source(cand), "target": _target(album)})

    @classmethod
    def verified(
        cls,
        request_id: int,
        candidate: dict[str, Any],
        cand: Candidate,
        paths: list[Path],
        album: AlbumInfo,
        *,
        unassigned: list[str],
        partial: str,
        acoustid: list[dict[str, Any]],
        duration_deltas: list[float],
        assignment: list[dict[str, Any]],
    ) -> "Evidence":
        """Stage 2: the downloaded files scored against a release."""
        return cls(
            {
                "request_id": request_id,
                "candidate": candidate,
                "unassigned": unassigned,
                "partial": partial,
                "source": _source(cand, paths),
                "target": _target(album),
                "acoustid": acoustid,
                "duration_deltas": duration_deltas,
                "assignment": assignment,
            }
        )

    def to_json(self) -> dict[str, Any]:
        return self.raw

    def with_unverified(self, edits: dict[str, Any]) -> "Evidence":
        return Evidence({**self.raw, "unverified": edits})

    def with_switched_from(self, release_id: str) -> "Evidence":
        return Evidence({**self.raw, "canonical_switched_from": release_id})

    @property
    def request_id(self) -> int:
        """0 when the decision did not come from a request (ids start at 1)."""
        return self.raw.get("request_id", 0)

    @property
    def files(self) -> list[dict[str, Any]]:
        return self.raw.get("source", {}).get("files", [])

    @property
    def peer(self) -> str:
        return self.raw["source"]["peer"]

    @property
    def folder(self) -> str:
        return self.raw["source"]["folder"]

    @property
    def paths(self) -> list[str]:
        return [f["local_path"] for f in self.raw["source"]["files"]]

    @property
    def candidate(self) -> dict[str, Any]:
        """The Candidate as stored; `requests.candidate_from_dict` turns it into one."""
        return self.raw["candidate"]

    @property
    def target(self) -> dict[str, Any]:
        return self.raw.get("target", {})

    @property
    def release_group_id(self) -> str | None:
        return self.target.get("release_group_id")

    @property
    def acoustid_recording_ids(self) -> list[str]:
        return [r for a in self.raw.get("acoustid", []) for r in a.get("recording_ids", [])]

    @property
    def unverified(self) -> dict[str, Any] | None:
        """The reviewer's tag edits when the files were marked unverified."""
        return self.raw.get("unverified")

    @property
    def partial(self) -> str:
        return self.raw.get("partial", "")

    @property
    def unassigned(self) -> tuple[str, ...]:
        return tuple(self.raw.get("unassigned", ()))

    @property
    def canonical_switched_from(self) -> str | None:
        """Set once an earlier import run made this release canonical."""
        return self.raw.get("canonical_switched_from")

    @property
    def assignment(self) -> list[dict[str, Any]]:
        """Rows of local_path, track, recording_id, acoustid_id. Empty for unverified edits,
        which import by their own tags. A missing key is an error."""
        return [] if self.unverified else self.raw["assignment"]

    def resolve(self, album: AlbumInfo) -> list[tuple[Path, TrackInfo | None, str | None]]:
        """Each assigned file with its track on `album` (None: the release changed since verify).
        Old rows have only the track position; newer ones also name the recording, which wins
        when the position no longer matches."""
        by_rec = {t.recording_id: t for t in album.tracks}
        out: list[tuple[Path, TrackInfo | None, str | None]] = []
        for a in self.assignment:
            pos: int = a["track"]
            track = album.tracks[pos] if pos < len(album.tracks) else None
            # shortcut: a recording on the release twice AND a shifted list maps to the last
            # one; upgrade by storing the medium and position in the assignment.
            if "recording_id" in a and (track is None or track.recording_id != a["recording_id"]):
                track = by_rec.get(a["recording_id"])
            out.append((Path(a["local_path"]), track, a["acoustid_id"]))
        return out


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
        "release_group_id": album.release_group_id,
        "tracks": [
            {"position": t.index, "title": t.title, "length": t.length} for t in album.tracks
        ],
    }
