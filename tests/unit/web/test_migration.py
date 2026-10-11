import csv
import io
import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.core.matching import Band, MatchResult
from mixsync.db import decisions
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.auth import User
from mixsync.db.models.work import Migration
from mixsync.db.models.work import Request as AlbumRequest

MBID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def sf(settings: Settings) -> sessionmaker[Session]:
    upgrade()
    return make_session_factory(make_engine(settings))


@pytest.fixture
def client(sf: sessionmaker[Session]) -> Iterator[TestClient]:
    with sf() as s, s.begin():
        create_user(s, "root", "pw", admin=True)
    with TestClient(create_app(sf, Settings()), follow_redirects=False) as c:
        assert c.post("/login", data={"username": "root", "password": "pw"}).status_code == 303
        yield c


@pytest.fixture
def migration_id(sf: sessionmaker[Session]) -> int:
    """A running migration with one imported request whose decision says it is partial."""
    with sf() as s, s.begin():
        uid = s.query(User).filter_by(username="root").one().id
        m = Migration(user_id=uid, status="running", summary={})
        s.add(m)
        s.flush()
        r = AlbumRequest(
            user_id=uid,
            release_mbid=MBID,
            artist="Alpha Band",
            album="Alpha Album",
            status="imported",
            migration_id=m.id,
        )
        s.add(r)
        s.flush()
        decisions.record(
            s,
            MatchResult(0.0, Band.AUTO_ACCEPT, (), (), 1),
            evidence={"request_id": r.id, "partial": "1 of 13 tracks present"},
            user_id=uid,
            stage=2,
            release_mbid=MBID,
        )
        return m.id


def test_csv_has_the_partial_row(client: TestClient, migration_id: int) -> None:
    r = client.get(f"/migration/{migration_id}/report.csv")
    assert r.status_code == 200
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [(x["status"], x["reason"]) for x in rows] == [("partial", "1 of 13 tracks present")]


def test_page_counts_the_partial_album(client: TestClient, migration_id: int) -> None:
    text = client.get("/migration").text
    assert re.search(r"<li>partial: 1</li>", text) and "<li>imported: 0</li>" in text
