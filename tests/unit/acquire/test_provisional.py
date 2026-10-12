"""Discovery (provisional) requests: the whole album is fetched and verified, only the wanted
recordings are imported, into /data/discover."""

import hashlib
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from mixsync.acquire.requests import create_request
from mixsync.core.errors import TransientError
from mixsync.core.jobs import JobKind
from mixsync.core.types import AcoustIdResult
from mixsync.db import canonical as canonical_db
from mixsync.db.models.discovery import ProvisionalTrack
from mixsync.db.models.library import CanonicalRelease, Track
from mixsync.db.models.work import Request

from .conftest import Env
from .fakes import ALBUM, BIG
from .test_pipeline import decision_rows, library, request, status

WANTED = ("Gamma-rec-3", "Gamma-rec-7")
FOLDER = "Music\\Gamma Band\\Gamma Album"


def discover(env: Env) -> list[str]:
    root = env.data_dir / "discover"
    return sorted(str(p.relative_to(root)) for p in root.rglob("*.mp3")) if root.exists() else []


def discovery(env: Env) -> int:
    with env.sessions.begin() as s:
        return create_request(
            s,
            env.clock.now(),
            env.user_id,
            str(BIG.release_id),
            provisional=True,
            recordings=WANTED,
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


async def test_a_review_band_match_fails_the_request_and_never_reaches_review(env: Env) -> None:
    env.source.add("peer1", FOLDER, BIG)
    env.acoustid.override["Gamma Album/02 Gamma Two.mp3"] = [
        AcoustIdResult("aid-x", 0.95, ("somebody-elses-recording",))
    ]
    rid = discovery(env)
    await env.drain(until=lambda: status(env, rid) == "failed")

    assert status(env, rid) == "failed"
    with env.sessions() as s:
        req = s.get(Request, rid)
        assert req and "review" in (req.last_error or "")
    assert [d.status for d in decision_rows(env, 2)] == ["rejected_auto"]
    assert discover(env) == [] and rows(env) == []
    assert len(tree(env.downloads)) == 10


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
