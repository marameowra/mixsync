import csv
import io
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

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


def _csv(client: TestClient, mid: int, query: str) -> list[dict[str, str]]:
    r = client.get(f"/migration/{mid}/report.csv{query}")
    assert r.status_code == 200
    return list(csv.DictReader(io.StringIO(r.text)))


def test_csv_start_and_end_filter_the_rows(client: TestClient, migration_id: int) -> None:
    today = datetime.now(UTC).date()
    day = timedelta(days=1)
    assert len(_csv(client, migration_id, f"?start={today}&end={today}")) == 1
    assert _csv(client, migration_id, f"?start={today + day}") == []
    assert _csv(client, migration_id, f"?end={today - day}") == []


@pytest.mark.parametrize(
    "query", ["?start=nope", "?end=2026-13-01", "?start=2026-02-01&end=2026-01-01"]
)
def test_bad_dates_are_rejected(client: TestClient, migration_id: int, query: str) -> None:
    assert client.get(f"/migration/{migration_id}/report.csv{query}").status_code in (400, 422)
    assert client.get(f"/migration{query}").status_code in (400, 422)


def test_page_prefills_the_dates_and_the_csv_link_carries_them(
    client: TestClient, migration_id: int
) -> None:
    text = client.get("/migration?start=2020-01-02&end=2030-03-04").text
    assert 'name="start" value="2020-01-02"' in text and 'name="end" value="2030-03-04"' in text
    assert f"/migration/{migration_id}/report.csv?start=2020-01-02&amp;end=2030-03-04" in text
    assert re.search(r'name="start" value="\d{4}-\d\d-\d\d"', client.get("/migration").text)
