"""The admin settings page (ADR 0021): save, reset, source badges, validation."""

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

RESTART = "Saved. Restart mixsync-web and mixsync-worker to apply."


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


def login(c: TestClient, user: str) -> str:
    assert c.post("/login", data={"username": user, "password": "pw"}).status_code == 303
    m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', c.get("/account").text)
    assert m
    return m.group(1)


def saved(sf: sessionmaker[Session], name: str) -> object:
    with sf() as s:
        row = s.get(AppSettings, 1)
        return getattr(row, name) if row else None


def field(page: str, name: str) -> str:
    """The HTML block of one field."""
    m = re.search(rf'<div class="field[^"]*">\s*<label for="f-{name}">.*?</div>', page, re.S)
    assert m
    return m.group(0)


def test_non_admins_get_403_on_get_and_post(client: TestClient) -> None:
    tok = login(client, "alice")
    assert client.get("/settings").status_code == 403
    assert client.post("/settings", data={"csrf_token": tok, "trash_days": "5"}).status_code == 403
    assert client.post("/settings/reset/trash_days", data={"csrf_token": tok}).status_code == 403
    assert 'href="/settings"' not in client.get("/account").text


def test_save_persists_then_reset_clears(client: TestClient, sf: sessionmaker[Session]) -> None:
    tok = login(client, "root")
    assert 'href="/settings"' in client.get("/account").text
    data = {
        "csrf_token": tok,
        "trash_days": "7",
        "adopt_downloads": "true",
        "canonical_media": "CD, Vinyl",
        "log_level": "debug",
        "mb_contact": "",
    }
    r = client.post("/settings", data=data)
    assert r.status_code == 303 and r.headers["location"] == "/settings?saved=1"
    assert RESTART in client.get("/settings?saved=1").text
    assert saved(sf, "trash_days") == 7 and saved(sf, "adopt_downloads") is True
    assert saved(sf, "canonical_media") == ["CD", "Vinyl"] and saved(sf, "log_level") == "DEBUG"
    assert saved(sf, "mb_contact") is None  # blank is not saved
    assert client.post("/settings", data={"csrf_token": tok}).status_code == 303
    assert saved(sf, "trash_days") == 7 and saved(sf, "slskd_url") is None  # blank: left as is
    assert client.post("/settings/reset/trash_days", data={"csrf_token": tok}).status_code == 303
    assert saved(sf, "trash_days") is None and saved(sf, "adopt_downloads") is True
    assert client.post("/settings/reset/nope", data={"csrf_token": tok}).status_code == 404


def test_source_badges_and_the_env_override_warning(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MIXSYNC_SLSKD_URL", "http://env:1")
    monkeypatch.setenv("MIXSYNC_TRASH_DAYS", "9")
    tok = login(client, "root")
    page = client.get("/settings").text
    assert "source: default" in field(page, "flac_level")
    env = field(page, "slskd_url")
    assert "source: environment" in env and "http://env:1" in env and "ignored" not in env
    client.post("/settings", data={"csrf_token": tok, "trash_days": "9", "flac_level": "5"})
    page = client.get("/settings").text
    assert "source: saved" in field(page, "flac_level")
    assert "MIXSYNC_FLAC_LEVEL" not in page  # no env var, no warning
    both = field(page, "trash_days")  # a saved value equal to env still counts as saved
    assert "source: saved" in both
    assert "MIXSYNC_TRASH_DAYS is set but ignored: the saved value wins." in both
    assert "ignored" not in field(page, "slskd_url")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("mb_rate", "2"),
        ("timezone", "Mars/Base"),
        ("log_level", "LOUD"),
        ("trash_days", "abc"),
        ("flac_level", "9"),
    ],
)
def test_an_invalid_value_is_rejected_and_nothing_is_saved(
    client: TestClient, sf: sessionmaker[Session], name: str, value: str
) -> None:
    tok = login(client, "root")
    data = {"csrf_token": tok, name: value, "slskd_max_searches": "4", "cap": "90"}
    r = client.post("/settings", data=data)
    assert r.status_code == 400 and "Nothing was saved" in r.text
    assert 'class="error"' in field(r.text, name) and f'value="{value}"' in field(r.text, name)
    assert saved(sf, "slskd_max_searches") is None
    assert saved(sf, "min_auto_accept_confidence") is None


def test_read_only_settings_are_shown_and_secrets_are_not(client: TestClient) -> None:
    login(client, "root")
    page = client.get("/settings").text
    assert "config_dir" in page and "data_dir" in page
    for secret in ("slskd_api_key", "navidrome_password", "acoustid", "secret_key", "database_url"):
        assert secret not in page
