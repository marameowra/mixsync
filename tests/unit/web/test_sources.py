"""The admin's download sources page: order with ↑/↓, enable or disable, admins only."""

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
from mixsync.db.models.auth import AppSettings
from mixsync.web.routes import account


@pytest.fixture
def sf(settings: Settings) -> sessionmaker[Session]:
    upgrade()
    sf = make_session_factory(make_engine(settings))
    with sf() as s, s.begin():
        create_user(s, "alice", "pw")
        create_user(s, "root", "pw", admin=True)
    return sf


@pytest.fixture
def two_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second download source, configured by no setting at all."""
    monkeypatch.setattr(
        account, "DOWNLOAD_SOURCES", {**account.DOWNLOAD_SOURCES, "other": ("Other", ())}
    )


def client_for(sf: sessionmaker[Session], settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(sf, settings), follow_redirects=False) as c:
        yield c


@pytest.fixture
def client(sf: sessionmaker[Session]) -> Iterator[TestClient]:
    yield from client_for(sf, Settings())


def login(c: TestClient, user: str) -> str:
    c.cookies.clear()
    assert c.post("/login", data={"username": user, "password": "pw"}).status_code == 303
    m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', c.get("/account").text)
    assert m
    return m.group(1)


def saved(sf: sessionmaker[Session]) -> object:
    with sf() as s:
        row = s.get(AppSettings, 1)
        return row.source_priority if row else None


def test_non_admins_get_403(client: TestClient, sf: sessionmaker[Session]) -> None:
    tok = login(client, "alice")
    assert client.get("/account/sources").status_code == 403
    data = {"csrf_token": tok, "enabled": "slskd"}
    assert client.post("/account/sources", data=data).status_code == 403
    assert saved(sf) is None


def test_never_saved_every_configured_source_is_on_soulseek_first(
    client: TestClient, two_sources: None
) -> None:
    login(client, "root")
    page = client.get("/account/sources").text
    assert page.index("Soulseek (slskd)") < page.index("Other")
    assert len(re.findall(r'name="enabled" value="\w+"[^>]* checked', page)) == 2
    assert "not configured" not in page


def test_up_and_down_reorder_and_the_boxes_enable(
    client: TestClient, sf: sessionmaker[Session], two_sources: None
) -> None:
    tok = login(client, "root")
    form = {"csrf_token": tok, "enabled": ["slskd", "other"], "up": "other"}
    assert client.post("/account/sources", data=form).status_code == 303
    assert saved(sf) == [["other", True], ["slskd", True]]
    page = client.get("/account/sources").text
    assert page.index("Other") < page.index("Soulseek (slskd)")
    form = {"csrf_token": tok, "enabled": ["slskd"], "down": "slskd"}  # slskd is last: stays
    client.post("/account/sources", data=form)
    assert saved(sf) == [["other", False], ["slskd", True]]
    r = client.post("/account/sources", data={"csrf_token": tok, "down": "other"})  # none on
    assert r.status_code == 400 and "Enable at least one download source." in r.text
    assert saved(sf) == [["other", False], ["slskd", True]]  # refused: nothing written


def test_a_source_without_its_settings_is_not_configured_and_stays_off(
    sf: sessionmaker[Session],
) -> None:
    for c in client_for(sf, Settings(slskd_url="")):
        tok = login(c, "root")
        page = c.get("/account/sources").text
        assert "not configured" in page
        assert re.search(r'name="enabled" value="slskd"[^>]* disabled', page)
        c.post("/account/sources", data={"csrf_token": tok, "enabled": "slskd"})
        assert saved(sf) == [["slskd", False]]


def test_a_post_without_the_csrf_token_is_refused(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    login(client, "root")
    assert client.post("/account/sources", data={"enabled": "slskd"}).status_code == 403
    assert saved(sf) is None
