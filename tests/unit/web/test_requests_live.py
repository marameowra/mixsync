import re
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.requests import create_request
from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.auth import User
from mixsync.db.models.work import Request as AlbumRequest

HX = {"HX-Request": "true"}
MBIDS = [f"11111111-1111-1111-1111-11111111111{i}" for i in range(1, 5)]
TRIGGER = 'hx-trigger="every 5s [!document.hidden]"'


@pytest.fixture
def sf(settings: Settings) -> sessionmaker[Session]:
    upgrade()
    return make_session_factory(make_engine(settings))


@pytest.fixture
def client(sf: sessionmaker[Session]) -> Iterator[TestClient]:
    with sf() as s, s.begin():
        create_user(s, "alice", "pw")
        create_user(s, "bob", "pw")
    with TestClient(create_app(sf), follow_redirects=False) as c:
        assert c.post("/login", data={"username": "alice", "password": "pw"}).status_code == 303
        yield c


def add(sf: sessionmaker[Session], user: str, mbid: str, status: str) -> int:
    with sf() as s, s.begin():
        uid = s.query(User).filter_by(username=user).one().id
        req = create_request(s, datetime.now(UTC), uid, mbid)
        req.status = status
        s.flush()
        return req.id


def set_status(sf: sessionmaker[Session], req_id: int, status: str) -> None:
    with sf() as s, s.begin():
        s.get(AlbumRequest, req_id).status = status  # pyright: ignore[reportOptionalMemberAccess]


def poll_url(html: str) -> str:
    m = re.search(r'hx-get="([^"]+)"', html)
    assert m
    return m.group(1).replace("&amp;", "&")


def test_page_with_in_progress_request_has_polling_live_region(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    add(sf, "alice", MBIDS[0], "downloading")
    html = client.get("/requests").text
    assert '<div class="live-region"' in html and TRIGGER in html
    assert html.count('class="live-region"') == 1  # one region for the whole table


def test_fragment_returns_updated_status_and_stops_when_none_in_progress(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    rid = add(sf, "alice", MBIDS[0], "downloading")
    url = poll_url(client.get("/requests").text)
    busy = client.get(url, headers=HX)
    assert "<html" not in busy.text and TRIGGER in busy.text and "downloading" in busy.text
    set_status(sf, rid, "imported")
    done = client.get(url, headers=HX).text
    assert "<html" not in done and "imported" in done and "downloading" not in done
    assert "hx-get" not in done and TRIGGER not in done


def test_page_with_nothing_in_progress_does_not_poll(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    add(sf, "alice", MBIDS[0], "imported")
    assert "hx-get" not in client.get("/requests").text


def test_order_unchanged_after_status_change(client: TestClient, sf: sessionmaker[Session]) -> None:
    first = add(sf, "alice", MBIDS[0], "downloading")
    second = add(sf, "alice", MBIDS[1], "imported")
    url = poll_url(client.get("/requests").text)
    set_status(sf, first, "imported")
    html = client.get(url, headers=HX).text
    assert html.index(f"<td>{second}</td>") < html.index(f"<td>{first}</td>")
    third = add(sf, "alice", MBIDS[2], "downloading")  # a new request waits for a reload
    assert f"<td>{third}</td>" not in client.get(url, headers=HX).text


def test_fragment_never_exposes_another_users_request(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    mine = add(sf, "alice", MBIDS[0], "downloading")
    theirs = add(sf, "bob", MBIDS[1], "downloading")
    html = client.get(f"/requests?ids={mine}&ids={theirs}", headers=HX).text
    assert f"<td>{mine}</td>" in html and f"<td>{theirs}</td>" not in html
