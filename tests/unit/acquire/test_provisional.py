"""Discovery (provisional) requests: the whole album is fetched and verified, only the wanted
recordings are imported, into /data/discover."""

import hashlib
from dataclasses import replace
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from mixsync.acquire.requests import create_request
from mixsync.core.errors import TransientError
from mixsync.core.jobs import JobKind
from mixsync.core.types import AcoustIdResult
from mixsync.db import canonical as canonical_db
from mixsync.db.models.discovery import DiscoveryPlaylist, ProvisionalHold, ProvisionalTrack
from mixsync.db.models.library import CanonicalRelease, Track
from mixsync.db.models.work import Request

from .conftest import Env
from .fakes import ALBUM, BIG
from .test_pipeline import decision_rows, library, request, status, tried

WANTED = ("Gamma-rec-3", "Gamma-rec-7")
FOLDER = "Music\\Gamma Band\\Gamma Album"


def discover(env: Env) -> list[str]:
    root = env.data_dir / "discover"
    return sorted(str(p.relative_to(root)) for p in root.rglob("*.mp3")) if root.exists() else []


def discovery(env: Env, wanted: tuple[str, ...] = WANTED) -> int:
    with env.sessions.begin() as s:
        return create_request(
            s,
            env.clock.now(),
            env.user_id,
            str(BIG.release_id),
            provisional=True,
            recordings=wanted,
        ).id


def tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def rows(env: Env) -> list[ProvisionalTrack]:
    with env.sessions() as s:
        return list(s.scalars(select(ProvisionalTrack).order_by(ProvisionalTrack.path)))


def provisional_row(env: Env, recording: str, path: str, state: str) -> None:
    with env.sessions.begin() as s:
        s.add(
            ProvisionalTrack(
                path=path,
                recording_mbid=recording,
                release_group_mbid=ALBUM.release_group_id,
                batch_id="b",
                imported_at=env.clock.now(),
                status=state,
            )
        )


def own(env: Env, recording: str) -> None:
    with env.sessions.begin() as s:
        s.add(
            Track(
                path=f"{recording}.mp3",
                recording_mbid=recording,
                release_group_mbid="elsewhere",
                status="verified",
                batch_id="old",
                imported_at=env.clock.now(),
            )
        )


async def test_only_the_wanted_recordings_are_imported_into_discover(env: Env) -> None:
    env.source.add("peer1", FOLDER, BIG)
    rid = discovery(env)
    assert [j.priority for j in env.jobs(JobKind.SEARCH)] == [1]
    await env.drain(until=lambda: status(env, rid) == "importing")
    before = tree(env.downloads)
    await env.drain()

    assert status(env, rid) == "imported"
    assert library(env) == []
    assert len(discover(env)) == 2
    assert {r.recording_mbid for r in rows(env)} == set(WANTED)
    assert {r.path for r in rows(env)} == set(discover(env))
    assert all(r.status == "active" and r.release_mbid == BIG.release_id for r in rows(env))
    with env.sessions() as s:
        assert list(s.scalars(select(Track))) == []
        assert list(s.scalars(select(CanonicalRelease))) == []
    assert env.metadata.browsed == []
    assert env.target.rescans == 1
    after = tree(env.downloads)  # the 2 imported sources are released, the 8 others untouched
    assert len(before) == 10 and len(after) == 8
    assert all(before[k] == v for k, v in after.items())
    assert not {"03 Gamma Three.mp3", "07 Gamma Seven.mp3"} & {Path(k).name for k in after}


async def test_wanted_recordings_already_owned_skip_the_search(env: Env) -> None:
    own(env, WANTED[0])
    provisional_row(env, WANTED[1], "x.mp3", "active")
    rid = discovery(env)
    await env.drain()
    assert status(env, rid) == "owned"
    assert env.source.queries == [] and env.source.enqueued == []


async def test_a_trashed_provisional_track_is_not_owned(env: Env) -> None:
    env.source.add("peer1", FOLDER, BIG)
    own(env, WANTED[0])
    provisional_row(env, WANTED[1], "x.mp3", "trashed")
    rid = discovery(env)
    await env.drain()
    assert status(env, rid) == "imported" and env.source.enqueued == ["peer1"]


async def test_a_provisional_track_does_not_make_a_manual_request_skip(env: Env) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    for t in ALBUM.tracks:
        provisional_row(env, t.recording_id or "", f"{t.title}.mp3", "active")
    rid = request(env)
    await env.drain()
    assert status(env, rid) == "imported" and len(library(env)) == 2
    with env.sessions() as s:
        assert canonical_db.get(s, str(ALBUM.release_group_id))


async def test_a_review_band_match_tries_the_next_peer_and_never_reaches_review(env: Env) -> None:
    env.source.add("peer1", FOLDER, BIG)
    env.source.add("peer2", FOLDER + " (clean)", BIG)  # its own folder: no override applies
    env.acoustid.override["Gamma Album/02 Gamma Two.mp3"] = [
        AcoustIdResult("aid-x", 0.95, ("somebody-elses-recording",))
    ]
    rid = discovery(env)
    await env.drain()

    assert status(env, rid) == "imported"
    assert tried(env, rid) == [["peer1", FOLDER]]
    assert [d.status for d in decision_rows(env, 2)] == ["rejected_auto", "auto_accepted"]
    assert len(discover(env)) == 2 and len(rows(env)) == 2
    assert len(tree(env.downloads)) == 10 + 8  # peer1's whole folder stays, peer2's 2 released


async def test_a_resume_records_a_copy_whose_recording_became_owned(env: Env) -> None:
    env.source.add("peer1", FOLDER, BIG)
    rid = discovery(env)
    await env.drain(until=lambda: status(env, rid) == "importing")
    (d,) = decision_rows(env, 2)
    # the worker copied file 3 and died before its row committed
    (copied,) = env.pipeline.importer.import_release(
        [(env.downloads / "Gamma Album" / "03 Gamma Three.mp3", BIG.tracks[2], None)],
        BIG,
        "verified",
        f"request-{rid}-decision-{d.id}",
        root="discover",
    )
    assert copied.rel and rows(env) == []
    own(env, WANTED[0])  # and the recording arrived elsewhere meanwhile
    await env.drain()

    assert status(env, rid) == "imported"
    assert sorted(r.recording_mbid for r in rows(env)) == sorted(WANTED)
    assert copied.rel in {r.path for r in rows(env)} and len(discover(env)) == 2


def test_the_active_path_index_ignores_trashed_rows_only(env: Env) -> None:
    for state in ("trashed", "trashed", "active"):
        provisional_row(env, "r", "same.mp3", state)
    with pytest.raises(IntegrityError):
        provisional_row(env, "r", "same.mp3", "active")


async def test_a_trashed_track_can_be_imported_again_under_its_path(env: Env) -> None:
    env.source.add("peer1", FOLDER, BIG)
    first = discovery(env)
    await env.drain()
    assert status(env, first) == "imported"
    paths = {r.path for r in rows(env)}
    with env.sessions.begin() as s:  # trashed: the file is gone, the row stays
        for r in s.scalars(select(ProvisionalTrack)):
            r.status = "trashed"
    for p in paths:
        (env.data_dir / "discover" / p).unlink()
    env.source.add("peer2", FOLDER, BIG)

    second = discovery(env)
    await env.drain()
    assert status(env, second) == "imported"
    active = [r for r in rows(env) if r.status == "active"]
    assert {r.path for r in active} == paths and len(active) == 2
    assert {p for p in discover(env)} == paths


async def test_an_owned_wanted_recording_is_not_imported_again(env: Env) -> None:
    env.source.add("peer1", FOLDER, BIG)
    rid = discovery(env)
    await env.drain(until=lambda: status(env, rid) == "importing")
    own(env, WANTED[0])  # another user's import landed meanwhile
    await env.drain()
    assert status(env, rid) == "imported"
    assert {r.recording_mbid for r in rows(env)} == {WANTED[1]} and len(discover(env)) == 1


async def test_wanted_recordings_missing_from_the_release(env: Env) -> None:
    env.source.add("peer1", FOLDER, BIG)
    none = discovery(env, ("not-on-it", "nor-this"))
    await env.drain()
    assert status(env, none) == "failed" and env.source.enqueued == []
    with env.sessions() as s:
        req = s.get(Request, none)
        assert req and "none of the 2 wanted" in (req.last_error or "")

    some = discovery(env, (WANTED[0], "not-on-it"))
    await env.drain()
    assert status(env, some) == "imported" and len(discover(env)) == 1
    with env.sessions() as s:
        req = s.get(Request, some)
        assert req and req.last_error == "1 of 2 wanted recordings not on this release"


async def test_a_recording_twice_on_a_release_is_imported_once(env: Env) -> None:
    dup = replace(BIG, tracks=(*BIG.tracks, replace(BIG.tracks[2], index=11, medium_index=11)))
    env.source.add("peer1", FOLDER, dup)
    rid = discovery(env)
    await env.drain()
    assert status(env, rid) == "imported"
    assert sorted(r.recording_mbid for r in rows(env)) == sorted(WANTED)


async def test_one_failing_file_releases_nothing(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.source.add("peer1", FOLDER, BIG)
    real = env.ops.import_file

    def flaky(src: Path, rel: str, write_tags: Any, batch: str, origin: Path, root: str) -> str:
        if "Seven" in src.name:
            raise OSError("disk full")
        return real(src, rel, write_tags, batch, origin, root)

    monkeypatch.setattr(env.ops, "import_file", flaky)
    rid = discovery(env)
    await env.drain()
    assert status(env, rid) == "failed" and len(tree(env.downloads)) == 10


def test_a_provisional_request_needs_recordings(env: Env) -> None:
    with env.sessions.begin() as s, pytest.raises(ValueError):
        create_request(s, env.clock.now(), env.user_id, "x", provisional=True)


async def test_a_retried_import_does_not_duplicate_files_or_rows(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.source.add("peer1", FOLDER, BIG)
    rid = discovery(env)
    real, calls = env.pipeline.importer.import_release, []

    def crashing(*a: Any) -> Any:
        calls.append(1)
        results = real(*a)
        if len(calls) == 1:  # the files are in, then the worker dies
            raise TransientError("killed")
        return results

    monkeypatch.setattr(env.pipeline.importer, "import_release", crashing)
    await env.drain()

    assert status(env, rid) == "imported" and len(calls) == 2
    assert len(discover(env)) == 2 and len(rows(env)) == 2
    assert len(tree(env.downloads)) == 8


async def test_a_discover_import_starts_the_holds_and_queues_pushes(env: Env) -> None:
    env.source.add("peer1", FOLDER, BIG)
    rid = discovery(env)
    with env.sessions.begin() as s:
        pl = DiscoveryPlaylist(user_id=env.user_id, name="Discovery", retention_days=5)
        s.add(pl)
        s.flush()
        pid = pl.id
        s.add(
            ProvisionalHold(
                user_id=env.user_id, recording_mbid=WANTED[0], playlist_id=pid, request_id=rid
            )
        )
    await env.drain()
    with env.sessions() as s:
        hold = s.scalars(select(ProvisionalHold)).one()
        assert hold.provisional_track_id is not None and hold.expires_at is not None
        imported = rows(env)[0].imported_at.replace(tzinfo=UTC)
        assert hold.expires_at.replace(tzinfo=UTC) == imported + timedelta(days=5)
    pushes = env.jobs(JobKind.DISCOVERY_PUSH)  # one soon, one for a scan that ran long
    assert [j.payload for j in pushes] == [{"playlist_id": pid}] * 2
