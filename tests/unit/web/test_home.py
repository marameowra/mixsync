import logging
import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.library import fragmentation
from mixsync.web.routes import home


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    upgrade()
    sf: sessionmaker[Session] = make_session_factory(make_engine(settings))
    with sf() as s, s.begin():
        create_user(s, "root", "pw", admin=True)
    with TestClient(create_app(sf), follow_redirects=False) as c:
        assert c.post("/login", data={"username": "root", "password": "pw"}).status_code == 303
        yield c


def test_home_scans_for_fragments_at_most_once_per_ttl(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    scans: list[int] = []

    def find(s: Session) -> list[fragmentation.Fragment]:
        scans.append(1)
        return [fragmentation.Fragment("g", None, {"a": 1, "b": 1})]

    monkeypatch.setattr(fragmentation, "find", find)
    for _ in range(3):
        page = client.get("/")
        assert page.status_code == 200 and "/library/fragments" in page.text
    assert len(scans) == 1
    monkeypatch.setattr(home, "FRAGMENTS_TTL", 0.0)  # the count is stale: scan again
    assert client.get("/").status_code == 200
    assert len(scans) == 2


def test_each_request_logs_its_method_path_status_and_time(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="mixsync.app"):
        assert client.get("/").status_code == 200
    lines = [r.getMessage() for r in caplog.records if r.name == "mixsync.app"]
    assert any(re.fullmatch(r"GET / 200 \d+\.\dms", m) for m in lines), lines
