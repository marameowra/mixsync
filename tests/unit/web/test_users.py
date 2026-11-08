"""Admin user management and the self-service password change."""

import re
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.db.auth import authenticate, create_user, is_admin
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.auth import Session as SessionRow
from mixsync.db.models.auth import User


@pytest.fixture
def sf(settings: Settings) -> sessionmaker[Session]:
    upgrade()
    sf = make_session_factory(make_engine(settings))
    with sf() as s, s.begin():
        create_user(s, "alice", "pw")
        create_user(s, "root", "pw", admin=True)
    return sf


@pytest.fixture
def app_client(sf: sessionmaker[Session]) -> Iterator[Callable[[], TestClient]]:
    clients: list[TestClient] = []

    def make() -> TestClient:
        c = TestClient(create_app(sf, Settings()), follow_redirects=False)
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.close()


def login(c: TestClient, user: str, password: str = "pw") -> str:
    c.cookies.clear()
    assert c.post("/login", data={"username": user, "password": password}).status_code == 303
    m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', c.get("/account").text)
    assert m
    return m.group(1)


def uid(sf: sessionmaker[Session], name: str) -> int:
    with sf() as s:
        return s.query(User).filter_by(username=name).one().id


def sessions(sf: sessionmaker[Session], name: str) -> int:
    with sf() as s:
        return s.query(SessionRow).filter_by(user_id=uid(sf, name)).count()


@pytest.fixture
def admin(app_client: Callable[[], TestClient]) -> tuple[TestClient, str]:
    c = app_client()
    return c, login(c, "root")


def test_users_page_lists_and_nav_link(admin: tuple[TestClient, str]) -> None:
    c, _ = admin
    r = c.get("/users")
    assert r.status_code == 200
    assert "alice" in r.text and "root" in r.text
    assert 'href="/users"' in r.text


def test_non_admin_gets_403_everywhere(
    app_client: Callable[[], TestClient], sf: sessionmaker[Session]
) -> None:
    c = app_client()
    tok = login(c, "alice")
    i = uid(sf, "alice")
    assert c.get("/users").status_code == 403
    assert 'href="/users"' not in c.get("/account").text
    posts = [
        ("/users", {"username": "x", "password": "y"}),
        (f"/users/{i}/status", {"status": "disabled"}),
        (f"/users/{i}/role", {"role": "admin"}),
        (f"/users/{i}/password", {"password": "y"}),
    ]
    for url, data in posts:
        assert c.post(url, data={"csrf_token": tok, **data}).status_code == 403
    assert not is_admin(sf(), i)


def test_add_user(admin: tuple[TestClient, str], sf: sessionmaker[Session]) -> None:
    c, tok = admin
    data = {"csrf_token": tok, "username": "carol", "password": "secret", "admin": "true"}
    assert c.post("/users", data=data).status_code == 303
    with sf() as s:
        assert authenticate(s, "carol", "secret") is not None
        assert is_admin(s, uid(sf, "carol"))
    dup = c.post("/users", data={**data, "admin": ""})
    assert dup.status_code == 400 and "already exists" in dup.text
    blank = c.post("/users", data={**data, "username": "d", "password": ""})
    assert blank.status_code == 400


def test_disable_and_enable(
    admin: tuple[TestClient, str],
    app_client: Callable[[], TestClient],
    sf: sessionmaker[Session],
) -> None:
    c, tok = admin
    a = app_client()
    login(a, "alice")
    i = uid(sf, "alice")
    assert (
        c.post(f"/users/{i}/status", data={"csrf_token": tok, "status": "disabled"}).status_code
        == 303
    )
    assert sessions(sf, "alice") == 0
    with sf() as s:
        assert authenticate(s, "alice", "pw") is None
    assert a.get("/account").status_code == 303
    assert (
        c.post(f"/users/{i}/status", data={"csrf_token": tok, "status": "active"}).status_code
        == 303
    )
    with sf() as s:
        assert authenticate(s, "alice", "pw") is not None


def test_change_role(admin: tuple[TestClient, str], sf: sessionmaker[Session]) -> None:
    c, tok = admin
    i = uid(sf, "alice")
    assert c.post(f"/users/{i}/role", data={"csrf_token": tok, "role": "admin"}).status_code == 303
    assert is_admin(sf(), i)
    assert c.post(f"/users/{i}/role", data={"csrf_token": tok, "role": "user"}).status_code == 303
    assert not is_admin(sf(), i)


def test_user_forms_confirm_with_htmx_and_show_a_rejection(
    admin: tuple[TestClient, str], sf: sessionmaker[Session]
) -> None:
    c, tok = admin
    page = c.get("/users").text
    assert page.count('hx-boost="true" hx-confirm="') == 3 * 2  # three forms for each of two users
    assert "onsubmit" not in page
    # a boosted form that is rejected returns a 400 page that carries the error
    r = c.post(
        f"/users/{uid(sf, 'root')}/status",
        data={"csrf_token": tok, "status": "disabled"},
        headers={"HX-Request": "true", "HX-Boosted": "true"},
    )
    assert r.status_code == 400 and "administrator must remain" in r.text
    assert r.headers["HX-Push-Url"] == "false"
    # a polling or Test-button request must not swap an error page in
    poll = c.get("/users/999/nope", headers={"HX-Request": "true"})
    assert poll.status_code == 404 and poll.headers["HX-Reswap"] == "none"
    boosted = c.get("/users/999/nope", headers={"HX-Request": "true", "HX-Boosted": "true"})
    assert "HX-Reswap" not in boosted.headers


def test_last_admin_cannot_be_demoted_or_disabled(
    admin: tuple[TestClient, str], sf: sessionmaker[Session]
) -> None:
    c, tok = admin
    me = uid(sf, "root")
    r = c.post(f"/users/{me}/role", data={"csrf_token": tok, "role": "user"})
    assert r.status_code == 400 and "administrator must remain" in r.text
    r = c.post(f"/users/{me}/status", data={"csrf_token": tok, "status": "disabled"})
    assert r.status_code == 400 and "administrator must remain" in r.text
    with sf() as s:
        assert is_admin(s, me)
        assert s.get(User, me).status == "active"  # type: ignore[union-attr]
    assert c.get("/users").status_code == 200
    # with a second active admin, either action goes through
    a = uid(sf, "alice")
    c.post(f"/users/{a}/role", data={"csrf_token": tok, "role": "admin"})
    assert c.post(f"/users/{me}/role", data={"csrf_token": tok, "role": "user"}).status_code == 303


def test_disabled_admin_does_not_count(
    admin: tuple[TestClient, str], sf: sessionmaker[Session]
) -> None:
    c, tok = admin
    a = uid(sf, "alice")
    c.post(f"/users/{a}/role", data={"csrf_token": tok, "role": "admin"})
    c.post(f"/users/{a}/status", data={"csrf_token": tok, "status": "disabled"})
    me = uid(sf, "root")
    r = c.post(f"/users/{me}/role", data={"csrf_token": tok, "role": "user"})
    assert r.status_code == 400


def test_admin_reset_ends_sessions(
    admin: tuple[TestClient, str],
    app_client: Callable[[], TestClient],
    sf: sessionmaker[Session],
) -> None:
    c, tok = admin
    a = app_client()
    login(a, "alice")
    assert sessions(sf, "alice") == 1
    r = c.post(f"/users/{uid(sf, 'alice')}/password", data={"csrf_token": tok, "password": "new"})
    assert r.status_code == 303
    assert sessions(sf, "alice") == 0
    assert a.get("/account").status_code == 303
    with sf() as s:
        assert authenticate(s, "alice", "new") is not None
        assert authenticate(s, "alice", "pw") is None
    blank = c.post(f"/users/{uid(sf, 'alice')}/password", data={"csrf_token": tok, "password": ""})
    assert blank.status_code == 400


def test_change_password(app_client: Callable[[], TestClient], sf: sessionmaker[Session]) -> None:
    here, other = app_client(), app_client()
    tok = login(here, "alice")
    login(other, "alice")
    assert sessions(sf, "alice") == 2

    def post(current: str, new: str, confirm: str) -> int:
        return here.post(
            "/account/password",
            data={"csrf_token": tok, "current": current, "new": new, "confirm": confirm},
        ).status_code

    assert post("wrong", "n", "n") == 400
    assert post("pw", "n", "different") == 400
    assert post("pw", "", "") == 400
    assert sessions(sf, "alice") == 2
    with sf() as s:
        assert authenticate(s, "alice", "pw") is not None

    assert post("pw", "n", "n") == 303
    assert sessions(sf, "alice") == 1
    assert here.get("/account").status_code == 200  # this session stays signed in
    assert other.get("/account").status_code == 303
    with sf() as s:
        assert authenticate(s, "alice", "n") is not None
        assert authenticate(s, "alice", "pw") is None


def test_password_inputs_autocomplete(admin: tuple[TestClient, str]) -> None:
    c, _ = admin
    account, users = c.get("/account").text, c.get("/users").text
    assert 'autocomplete="current-password"' in account
    assert account.count('autocomplete="new-password"') == 2
    assert users.count('autocomplete="new-password"') >= 2
    assert 'autocomplete="current-password"' not in users


def test_forms_need_csrf(admin: tuple[TestClient, str]) -> None:
    c, _ = admin
    assert c.post("/users", data={"username": "x", "password": "y"}).status_code == 403
    assert (
        c.post("/account/password", data={"current": "pw", "new": "n", "confirm": "n"}).status_code
        == 403
    )


def test_unknown_user_is_404(admin: tuple[TestClient, str]) -> None:
    c, tok = admin
    for leaf, data in (("status", {"status": "active"}), ("role", {"role": "user"})):
        assert c.post(f"/users/999/{leaf}", data={"csrf_token": tok, **data}).status_code == 404
    assert (
        c.post("/users/999/password", data={"csrf_token": tok, "password": "x"}).status_code == 404
    )


def test_reenable_does_not_revive_sessions(
    admin: tuple[TestClient, str],
    app_client: Callable[[], TestClient],
    sf: sessionmaker[Session],
) -> None:
    c, tok = admin
    a = app_client()
    login(a, "alice")
    i = uid(sf, "alice")
    c.post(f"/users/{i}/status", data={"csrf_token": tok, "status": "disabled"})
    c.post(f"/users/{i}/status", data={"csrf_token": tok, "status": "active"})
    assert a.get("/account").status_code == 303


def test_change_password_is_rate_limited(
    app_client: Callable[[], TestClient], sf: sessionmaker[Session]
) -> None:
    c = app_client()
    tok = login(c, "alice")

    def post(current: str) -> int:
        data = {"csrf_token": tok, "current": current, "new": "n", "confirm": "n"}
        return c.post("/account/password", data=data).status_code

    assert [post("wrong") for _ in range(10)] == [400] * 10
    assert post("pw") == 429  # even the right password is refused once locked
    with sf() as s:
        assert authenticate(s, "alice", "pw") is not None
