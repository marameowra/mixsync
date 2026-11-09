"""The admin trash page and the early purge (dry-run plan, typed confirmation)."""

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.library import Plan
from mixsync.db.models.safety import FileOp
from mixsync.library.fileops import FileOps

OLD, MID = "2020-01-01", "2020-01-02"


@pytest.fixture
def sf(settings: Settings) -> sessionmaker[Session]:
    upgrade()
    sf = make_session_factory(make_engine(settings))
    with sf() as s, s.begin():
        create_user(s, "alice", "pw")
        create_user(s, "root", "pw", admin=True)
    return sf


@pytest.fixture
def client(sf: sessionmaker[Session], settings: Settings, tmp_path: Path) -> Iterator[TestClient]:
    (tmp_path / "data" / "library").mkdir(parents=True)
    settings.data_dir = str(tmp_path / "data")
    with TestClient(create_app(sf, settings), follow_redirects=False) as c:
        yield c


def fo(c: TestClient) -> FileOps:
    ops: FileOps = c.app.state.services.fileops  # type: ignore[attr-defined]
    return ops


def put(c: TestClient, rel: str, data: bytes = b"xx") -> Path:
    p = fo(c).trash_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def protect(c: TestClient, sf: sessionmaker[Session]) -> tuple[str, Path]:
    """Today's folder, holding the trash of a done plan that can still be undone."""
    src = fo(c).library / "A/b.mp3"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"orig")
    rel = fo(c).trash("A/b.mp3", "merge-1")
    with sf.begin() as s:
        s.add(
            Plan(
                kind="merge",
                release_group_mbid="g",
                canonical_release_mbid="r",
                batch_id="merge-1",
                status="done",
            )
        )
    return datetime.now(UTC).date().isoformat(), fo(c).trash_dir / rel


def login(c: TestClient, user: str = "root") -> str:
    c.cookies.clear()
    assert c.post("/login", data={"username": user, "password": "pw"}).status_code == 303
    m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', c.get("/account").text)
    assert m
    return m.group(1)


def purge_ops(sf: sessionmaker[Session]) -> list[str]:
    with sf() as s:
        return sorted(s.scalars(select(FileOp.src).where(FileOp.kind == "purge")))


def test_admin_sees_each_folder_with_size_files_and_protected_flag(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    put(client, f"{OLD}/a.mp3", b"x" * 1_500_000)
    put(client, f"{OLD}/.discover/b.mp3", b"x" * 500_000)
    today, _ = protect(client, sf)
    login(client)
    r = client.get("/trash")
    assert r.status_code == 200 and 'href="/trash"' in r.text
    cell = r"\s*<td>(.*?)</td>"
    rows = re.findall("<tr>" + cell * 4, r.text)
    assert rows == [(today, "1", "0.0 MB", "Yes"), (OLD, "2", "2.0 MB", "No")]
    assert f'value="{OLD}"' in r.text
    assert f'value="{today}"' not in r.text  # a protected folder has no purge option


def test_plan_is_a_dry_run_and_leaves_out_protected_folders(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    a = put(client, f"{OLD}/a.mp3")
    today, kept = protect(client, sf)
    login(client)
    r = client.get("/trash/plan", params={"day": [OLD, today]})
    assert r.status_code == 200
    assert "Type 1 to confirm" in r.text
    assert f'name="day" value="{OLD}"' in r.text and f'value="{today}"' not in r.text
    assert a.exists() and kept.exists() and purge_ops(sf) == []
    assert client.get("/trash/plan", params={"day": [today]}).status_code == 400


def test_wrong_or_missing_confirmation_is_400_and_deletes_nothing(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    a = put(client, f"{OLD}/a.mp3")
    tok = login(client)
    for count in ("", "2", "one"):
        r = client.post("/trash/purge", data={"csrf_token": tok, "day": [OLD], "count": count})
        assert r.status_code == 400
    assert a.exists() and purge_ops(sf) == []


def test_confirmed_purge_removes_only_chosen_folders_each_file_journaled(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    a = put(client, f"{OLD}/a.mp3")
    b = put(client, f"{OLD}/.discover/b.mp3")
    other = put(client, f"{MID}/c.mp3")
    tok = login(client)
    r = client.post("/trash/purge", data={"csrf_token": tok, "day": [OLD], "count": "2"})
    assert r.status_code == 303 and r.headers["location"] == "/trash?purged=2"
    assert not a.exists() and not b.exists() and other.exists()
    assert purge_ops(sf) == [f"{OLD}/.discover/b.mp3", f"{OLD}/a.mp3"]
    assert "Purged 2 files." in client.get(r.headers["location"]).text


def test_crafted_post_for_a_protected_folder_is_skipped_and_reported(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    a = put(client, f"{OLD}/a.mp3")
    today, kept = protect(client, sf)
    tok = login(client)
    r = client.post("/trash/purge", data={"csrf_token": tok, "day": [OLD, today], "count": "2"})
    assert r.status_code == 303
    assert r.headers["location"] == f"/trash?purged=1&skipped={today}"
    assert kept.read_bytes() == b"orig" and not a.exists()
    assert purge_ops(sf) == [f"{OLD}/a.mp3"]
    assert f"Skipped, now protected: {today}." in client.get(r.headers["location"]).text


def test_unknown_folder_is_refused(client: TestClient, sf: sessionmaker[Session]) -> None:
    tok = login(client)
    for day in ("../library", "2020-01-03"):
        r = client.post("/trash/purge", data={"csrf_token": tok, "day": [day], "count": "0"})
        assert r.status_code == 400
    assert purge_ops(sf) == []


def test_non_admin_gets_403_on_page_plan_and_post(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    a = put(client, f"{OLD}/a.mp3")
    tok = login(client, "alice")
    assert client.get("/trash").status_code == 403
    assert client.get("/trash/plan", params={"day": [OLD]}).status_code == 403
    r = client.post("/trash/purge", data={"csrf_token": tok, "day": [OLD], "count": "1"})
    assert r.status_code == 403
    assert a.exists() and purge_ops(sf) == []
    assert 'href="/trash"' not in client.get("/account").text
