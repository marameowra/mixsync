"""The admin's worker status page: last seen, and a warning when the heartbeat is stale."""

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.infra import WorkerHeartbeat


@pytest.fixture
def sf(settings: Settings) -> sessionmaker[Session]:
    upgrade()
    sf = make_session_factory(make_engine(settings))
    with sf() as s, s.begin():
        create_user(s, "alice", "pw")
        create_user(s, "root", "pw", admin=True)
    return sf


@pytest.fixture
def client(sf: sessionmaker[Session]) -> Iterator[TestClient]:
    with TestClient(create_app(sf, Settings()), follow_redirects=False) as c:
        yield c


def login(c: TestClient, user: str) -> None:
    c.cookies.clear()
    assert c.post("/login", data={"username": user, "password": "pw"}).status_code == 303


def beat(sf: sessionmaker[Session], ago: timedelta) -> None:
    with sf() as s, s.begin():
        s.merge(WorkerHeartbeat(name="w", seen_at=datetime.now(UTC) - ago))


def test_only_admins_see_the_worker_page(client: TestClient) -> None:
    login(client, "alice")
    assert client.get("/account/worker").status_code == 403


def test_a_missing_heartbeat_warns(client: TestClient) -> None:
    login(client, "root")
    page = client.get("/account/worker").text
    assert "never recorded a heartbeat" in page and 'role="alert"' in page


def test_a_fresh_heartbeat_shows_last_seen_without_a_warning(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    beat(sf, timedelta(seconds=5))
    login(client, "root")
    page = client.get("/account/worker").text
    assert re.search(r"Worker last seen [5-9] s ago", page)
    assert 'role="alert"' not in page


def test_a_stale_heartbeat_warns(client: TestClient, sf: sessionmaker[Session]) -> None:
    beat(sf, timedelta(minutes=10))
    login(client, "root")
    page = client.get("/account/worker").text
    assert "Worker last seen 600 s ago" in page or "Worker last seen 601 s ago" in page
    assert "looks stopped or stuck" in page
