import json
from pathlib import Path
from typing import Any

from mixsync.acquire.evidence import Evidence
from mixsync.core.matching import AlbumInfo, Candidate, CandidateFile, TrackInfo

ALBUM = AlbumInfo(
    "Artist",
    "Album",
    (TrackInfo("One", index=0, recording_id="r1"), TrackInfo("Two", index=1, recording_id="r2")),
    release_group_id="rg1",
)
CAND = Candidate("peer", "Music\\Album", (CandidateFile("Music\\Album\\01.mp3", 10, "mp3"),))
ROW = {"local_path": "/dl/01.mp3", "track": 0, "recording_id": "r1", "acoustid_id": None}


def _verified(assignment: list[dict[str, Any]]) -> Evidence:
    return Evidence.verified(
        7,
        {"username": "peer"},
        CAND,
        [Path("/dl/01.mp3")],
        ALBUM,
        unassigned=["/dl/x.mp3"],
        partial="1 of 2 tracks present",
        acoustid=[{"score": 0.9, "recording_ids": ["r1"]}],
        duration_deltas=[0.5],
        assignment=assignment,
    )


def test_round_trip_through_json_keeps_the_stored_shape() -> None:
    stored = json.loads(json.dumps(_verified([ROW]).to_json()))
    assert set(stored) == {
        "request_id",
        "candidate",
        "unassigned",
        "partial",
        "source",
        "target",
        "acoustid",
        "duration_deltas",
        "assignment",
    }
    assert stored["source"]["files"][0]["local_path"] == "/dl/01.mp3"
    assert stored["target"]["tracks"][0] == {"position": 0, "title": "One", "length": None}
    ev = Evidence.parse(stored)
    assert (ev.request_id, ev.peer, ev.folder) == (7, "peer", "Music\\Album")
    assert ev.paths == ["/dl/01.mp3"]
    assert (ev.candidate, ev.partial, ev.unassigned) == (
        {"username": "peer"},
        "1 of 2 tracks present",
        ("/dl/x.mp3",),
    )
    assert (ev.release_group_id, ev.acoustid_recording_ids) == ("rg1", ["r1"])
    assert ev.unverified is None and ev.canonical_switched_from is None
    assert ev.assignment == [ROW]


def test_searched_has_the_stage_one_shape() -> None:
    stored = Evidence.searched(3, CAND, ALBUM).to_json()
    assert set(stored) == {"request_id", "source", "target"}
    assert Evidence.parse(stored).files[0]["local_path"] is None


def test_edits_and_switch_are_stored_and_read_back() -> None:
    ev = _verified([ROW]).with_switched_from("cid")
    assert ev.canonical_switched_from == "cid"
    edited = Evidence.parse(ev.with_unverified({"album": "A", "tracks": []}).to_json())
    assert edited.unverified == {"album": "A", "tracks": []}
    assert edited.assignment == []  # unverified imports by its edits, not the assignment
    assert edited.canonical_switched_from == "cid"


def test_resolve_prefers_the_recording_when_the_release_shifted() -> None:
    rows = [
        {"local_path": "/dl/01.mp3", "track": 0, "recording_id": "r2", "acoustid_id": "a"},
        {"local_path": "/dl/02.mp3", "track": 5, "recording_id": "gone", "acoustid_id": None},
    ]
    one, two = _verified(rows).resolve(ALBUM)
    assert one == (Path("/dl/01.mp3"), ALBUM.tracks[1], "a")
    assert two[1] is None  # the release changed since verify


def test_old_rows_without_recording_id_resolve_by_position() -> None:
    old = {"local_path": "/dl/02.mp3", "track": 1, "acoustid_id": None}
    stored = {"source": {"files": []}, "assignment": [old]}  # no request_id, partial, unassigned
    ev = Evidence.parse(stored)
    assert ev.resolve(ALBUM) == [(Path("/dl/02.mp3"), ALBUM.tracks[1], None)]
    assert (ev.request_id, ev.partial, ev.unassigned, ev.files) == (0, "", (), [])
