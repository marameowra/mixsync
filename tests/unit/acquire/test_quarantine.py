"""Dropped candidates move from /downloads into mixsync's quarantine on /data (ADR 0007)."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mixsync.acquire.evidence import Evidence
from mixsync.core.errors import SafetyError
from mixsync.core.types import AcoustIdResult
from mixsync.db.models.safety import FileOp

from .conftest import Env, login
from .fakes import ALBUM
from .test_adopter import Rig
from .test_pipeline import decision_rows, library, quarantined, request, status, veto_second_file

FOLDER = "Music\\Alpha Band\\Alpha Album"


def file_ops(env: Env, kind: str) -> list[FileOp]:
    with env.sessions() as s:
        return list(s.scalars(select(FileOp).where(FileOp.kind == kind).order_by(FileOp.id)))


def reject_first_peer(env: Env) -> None:
    mistagged = replace(ALBUM, artist="Zzzz Qqqq", album="Xxxx Wwww")
    cand = env.source.add("peer1", FOLDER, mistagged)
    env.source.catalog = {"alpha album": [cand]}  # found by the requested name
    for name in ("01 Song One.mp3", "02 Song Two.mp3"):
        env.acoustid.override[f"Alpha Album/{name}"] = [AcoustIdResult("aid-x", 0.1, ("nope",))]


async def test_a_rejected_candidate_is_quarantined_journaled_and_released(env: Env) -> None:
    reject_first_peer(env)
    rid = request(env)
    await env.drain(until=lambda: bool(decision_rows(env, 2)) and status(env, rid) != "verifying")
    (d,) = decision_rows(env, 2)
    assert d.band == "reject"
    day = env.clock.now().date().isoformat()
    kept = quarantined(env, "rejected")
    assert [p.name for p in kept] == ["01 Song One.mp3", "02 Song Two.mp3"]
    assert all(p.is_relative_to(env.data_dir / "quarantine" / "rejected" / day) for p in kept)
    assert list(env.downloads.iterdir()) == []  # the source is released
    assert {Path(p) for p in Evidence.parse(d.evidence).paths} == set(kept)
    copies = [o for o in file_ops(env, "copy") if o.root == "quarantine"]
    releases = [o for o in file_ops(env, "release") if o.root == "quarantine"]
    assert len(copies) == len(releases) == 2
    assert {o.status for o in copies + releases} == {"done"}
    assert {o.dst for o in copies} == {
        str(p.relative_to(env.data_dir / "quarantine")) for p in kept
    }


async def test_a_review_candidate_in_quarantine_can_be_accepted(env: Env, web: TestClient) -> None:
    env.source.add("peer1", FOLDER)
    veto_second_file(env)
    rid = request(env)
    await env.drain()
    (d,) = decision_rows(env, 2)
    assert status(env, rid) == "review" and list(env.downloads.iterdir()) == []
    held = quarantined(env, "review")
    assert len(held) == 2 and {Path(p) for p in Evidence.parse(d.evidence).paths} == set(held)

    tok = login(web, "alice")
    assert web.post(f"/review/{d.id}/accept", data={"csrf_token": tok}).status_code == 303
    await env.drain()

    assert status(env, rid) == "imported" and len(library(env)) == 2
    assert quarantined(env, "review") == []  # imported and released, like a download


async def test_rejected_folders_already_in_downloads_are_left_alone(env: Env) -> None:
    old = env.downloads / "Old Rejected"
    old.mkdir()
    (old / "01.mp3").write_bytes(b"audio")
    reject_first_peer(env)
    rid = request(env)
    await env.drain(until=lambda: bool(decision_rows(env, 2)) and status(env, rid) != "verifying")
    assert [p.name for p in old.iterdir()] == ["01.mp3"] and (
        old / "01.mp3"
    ).read_bytes() == b"audio"


async def test_the_adopter_does_not_pick_up_quarantine(env: Env, tmp_path: Path) -> None:
    env.source.add("peer1", FOLDER)
    veto_second_file(env)
    rid = request(env)
    await env.drain()
    assert status(env, rid) == "review" and len(quarantined(env, "review")) == 2
    rig = Rig(env, tmp_path)
    env.clock.advance(timedelta(minutes=15))
    await rig.scan()
    assert [r.source for r in rig.requests()] == ["manual"]


def test_quarantine_refuses_unsafe_paths(env: Env, tmp_path: Path) -> None:
    src = env.downloads / "Album" / "01.mp3"
    src.parent.mkdir()
    src.write_bytes(b"audio")
    with pytest.raises(SafetyError):
        env.ops.quarantine(src, "../escape", "b")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (env.data_dir / "quarantine").symlink_to(elsewhere)  # a symlinked quarantine root
    with pytest.raises(SafetyError):
        env.ops.quarantine(src, "rejected", "b")
    assert src.read_bytes() == b"audio" and list(elsewhere.rglob("*.mp3")) == []


def test_a_file_outside_downloads_is_never_moved(env: Env, tmp_path: Path) -> None:
    mine = tmp_path / "library-old" / "01.mp3"
    mine.parent.mkdir()
    mine.write_bytes(b"audio")
    assert env.ops.quarantine(mine, "rejected", "b") == mine and mine.read_bytes() == b"audio"
