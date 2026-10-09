from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mixsync.app import create_app
from mixsync.core.capabilities import Capability
from mixsync.core.config import Settings
from mixsync.db.auth import create_session, create_user, hash_password, verify_password
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.auth import User
from mixsync.web.deps import SESSION_COOKIE, require


@pytest.fixture
def sf(settings: Settings) -> sessionmaker[Session]:
    upgrade()
    return make_session_factory(make_engine(settings))


@pytest.fixture
def client(sf: sessionmaker[Session]) -> Iterator[TestClient]:
    with sf() as s, s.begin():
        create_user(s, "alice", "pw-alice")
        create_user(s, "root", "pw-root", admin=True)
        create_user(s, "off", "pw-off")
        s.query(User).filter_by(username="off").one().status = "disabled"
    app: FastAPI = create_app(sf)
    app.add_api_route(
        "/needs-admin", lambda: "ok", dependencies=[Depends(require(Capability.admin))]
    )
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _login(c: TestClient, user: str, pw: str) -> httpx.Response:
    return c.post("/login", data={"username": user, "password": pw})


def test_password_verify() -> None:
    h = hash_password("secret")
    assert verify_password(h, "secret")
    assert not verify_password(h, "nope")
    assert not verify_password("garbage", "secret")


def test_login_html_has_autocomplete(client: TestClient) -> None:
    html = client.get("/login").text
    assert 'autocomplete="username"' in html
    assert 'autocomplete="current-password"' in html


def test_anonymous_redirected(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_login_success_and_logout(client: TestClient) -> None:
    r = _login(client, "alice", "pw-alice")
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "alice" in client.get("/").text
    assert client.post("/logout").status_code == 303
    assert client.get("/").status_code == 303


@pytest.mark.parametrize(("user", "pw"), [("alice", "bad"), ("nobody", "x"), ("off", "pw-off")])
def test_login_rejected(client: TestClient, user: str, pw: str) -> None:
    r = _login(client, user, pw)
    assert r.status_code == 401
    assert SESSION_COOKIE not in r.cookies


def test_require_403(client: TestClient) -> None:
    _login(client, "alice", "pw-alice")
    assert client.get("/needs-admin").status_code == 403


def test_require_allows_admin(client: TestClient) -> None:
    _login(client, "root", "pw-root")
    assert client.get("/needs-admin").status_code == 200


def test_expired_session_rejected(client: TestClient, sf: sessionmaker[Session]) -> None:
    with sf() as s, s.begin():
        uid = s.query(User).filter_by(username="alice").one().id
        token = create_session(s, uid, datetime.now(UTC), timedelta(seconds=-1))
    client.cookies.set(SESSION_COOKIE, token)
    assert client.get("/").status_code == 303
