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
from mixsync.db.models.auth import User
from mixsync.web.routes.account import get_link

SECRET = "s3cret-token-XYZ"


class FakeValidator:
    def __init__(self) -> None:
        self.ok = True

    async def listenbrainz_username(self, token: str) -> str | None:
        return "lb-real-name" if self.ok else None

    async def navidrome_ping(self, username: str, password: str) -> bool:
        return self.ok


@pytest.fixture
def sf(settings: Settings) -> sessionmaker[Session]:
    upgrade()
    sf = make_session_factory(make_engine(settings))
    with sf() as s, s.begin():
        create_user(s, "alice", "pw")
        create_user(s, "bob", "pw")
    return sf


@pytest.fixture
def fake() -> FakeValidator:
    return FakeValidator()


@pytest.fixture
def client(sf: sessionmaker[Session], fake: FakeValidator) -> Iterator[TestClient]:
    app = create_app(sf, Settings())
    app.state.link_validator = fake
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _login(c: TestClient, user: str) -> str:
    c.cookies.clear()
    assert c.post("/login", data={"username": user, "password": "pw"}).status_code == 303
    m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', c.get("/account").text)
    assert m
    return m.group(1)


def _link(sf: sessionmaker[Session], user: str, service: str) -> tuple[str, str] | None:
    with sf() as s:
        uid = s.query(User).filter_by(username=user).one().id
        link = get_link(s, uid, service)
        return (link.username, link.secret) if link else None


def test_save_and_display_never_leaks_secret(client: TestClient, sf: sessionmaker[Session]) -> None:
    tok = _login(client, "alice")
    r = client.post("/account/listenbrainz", data={"secret": SECRET, "csrf_token": tok})
    assert r.status_code == 303
    assert _link(sf, "alice", "listenbrainz") == ("lb-real-name", SECRET)  # validated name wins
    r = client.post(
        "/account/navidrome", data={"username": "ali", "secret": SECRET, "csrf_token": tok}
    )
    assert r.status_code == 303
    page = client.get("/account")
    assert "Linked as lb-real-name" in page.text and "Linked as ali" in page.text
    assert "Replace" in page.text and "Unlink" in page.text
    assert SECRET not in page.text and SECRET not in r.text


def test_failed_validation_stores_nothing(
    client: TestClient, sf: sessionmaker[Session], fake: FakeValidator
) -> None:
    tok = _login(client, "alice")
    fake.ok = False
    r = client.post("/account/listenbrainz", data={"secret": SECRET, "csrf_token": tok})
    assert r.status_code == 400 and "did not accept" in r.text and SECRET not in r.text
    r = client.post(
        "/account/navidrome", data={"username": "ali", "secret": SECRET, "csrf_token": tok}
    )
    assert r.status_code == 400 and "did not accept" in r.text and SECRET not in r.text
    assert _link(sf, "alice", "listenbrainz") is None and _link(sf, "alice", "navidrome") is None


def test_failed_replace_keeps_old_link(
    client: TestClient, sf: sessionmaker[Session], fake: FakeValidator
) -> None:
    tok = _login(client, "alice")
    client.post("/account/listenbrainz", data={"secret": SECRET, "csrf_token": tok})
    fake.ok = False
    client.post("/account/listenbrainz", data={"secret": "other", "csrf_token": tok})
    assert _link(sf, "alice", "listenbrainz") == ("lb-real-name", SECRET)


def test_users_are_isolated(client: TestClient, sf: sessionmaker[Session]) -> None:
    tok = _login(client, "alice")
    client.post("/account/listenbrainz", data={"secret": SECRET, "csrf_token": tok})
    tok = _login(client, "bob")
    page = client.get("/account")
    assert "lb-real-name" not in page.text and "Not linked" in page.text
    client.post("/account/listenbrainz/unlink", data={"csrf_token": tok})  # touches only bob's
    assert _link(sf, "alice", "listenbrainz") == ("lb-real-name", SECRET)
    client.post("/account/listenbrainz", data={"secret": "bobs", "csrf_token": tok})
    assert _link(sf, "alice", "listenbrainz") == ("lb-real-name", SECRET)
    assert _link(sf, "bob", "listenbrainz") == ("lb-real-name", "bobs")


def test_csrf_required(client: TestClient, sf: sessionmaker[Session]) -> None:
    _login(client, "alice")
    assert client.post("/account/listenbrainz", data={"secret": SECRET}).status_code == 403
    assert _link(sf, "alice", "listenbrainz") is None


def test_unlink_and_unknown_service(client: TestClient, sf: sessionmaker[Session]) -> None:
    tok = _login(client, "alice")
    client.post("/account/listenbrainz", data={"secret": SECRET, "csrf_token": tok})
    r = client.post("/account/listenbrainz/unlink", data={"csrf_token": tok})
    assert r.status_code == 303 and _link(sf, "alice", "listenbrainz") is None
    r = client.post("/account/lastfm", data={"secret": "x", "csrf_token": tok})
    assert r.status_code == 404


def test_requires_login(client: TestClient) -> None:
    assert client.get("/account").status_code == 303
