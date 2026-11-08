"""The admin settings page (ADR 0021): save, reset, source badges, validation."""

import re
from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from mixsync.app import create_app
from mixsync.core.config import PATH_FIELDS, Settings
from mixsync.db.app_settings import EDITABLE, load_settings
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.auth import AppSettings
from mixsync.db.secretbox import SecretBox
from mixsync.library.paths import render
from mixsync.web.routes.settings import GROUPS

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
    assert client.post("/settings/clear/slskd_api_key", data={"csrf_token": tok}).status_code == 403
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
        "canonical_countries": " , ",
    }
    r = client.post("/settings", data=data)
    assert r.status_code == 303 and r.headers["location"] == "/settings?saved=1"
    assert RESTART in client.get("/settings?saved=1").text
    assert saved(sf, "trash_days") == 7 and saved(sf, "adopt_downloads") is True
    assert saved(sf, "canonical_media") == ["CD", "Vinyl"] and saved(sf, "log_level") == "DEBUG"
    assert saved(sf, "mb_contact") is None  # blank is not saved
    assert saved(sf, "canonical_countries") is None  # only commas: blank too
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
        ("path_template", "{artist}.{ext}"),
        ("path_template", "{title}.flac"),
        ("path_template", "{title}.{ext}.{ext}"),
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


def test_read_only_settings_are_shown_and_bootstrap_secrets_are_not(client: TestClient) -> None:
    login(client, "root")
    page = client.get("/settings").text
    assert "config_dir" in page and "data_dir" in page
    for name in ("secret_key", "database_url"):
        assert name not in page


SECRET = "s3cr3t-value-xyz"


def _ciphertext(sf: sessionmaker[Session], name: str) -> str | None:
    value = saved(sf, f"{name}_encrypted")
    assert value is None or isinstance(value, str)
    return value


def test_a_saved_secret_is_ciphertext_and_never_rendered(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    tok = login(client, "root")
    r = client.post("/settings", data={"csrf_token": tok, "slskd_api_key": SECRET})
    assert r.status_code == 303
    token = _ciphertext(sf, "slskd_api_key")
    assert token and SECRET not in token
    assert load_settings().slskd_api_key == SECRET
    with sf() as s:
        assert s.execute(text("SELECT slskd_api_key_encrypted FROM app_settings")).scalar() == token
    page = client.get("/settings?saved=1").text
    assert SECRET not in page and token not in page
    block = field(page, "slskd_api_key")
    assert (
        "saved" in block and 'type="password"' in block and 'autocomplete="new-password"' in block
    )
    assert 'value=""' in block and "placeholder" not in block
    assert "not set" in field(page, "navidrome_password")


def test_blank_keeps_a_new_value_replaces_and_clear_removes(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    tok = login(client, "root")
    client.post("/settings", data={"csrf_token": tok, "navidrome_password": SECRET})
    first = _ciphertext(sf, "navidrome_password")
    client.post("/settings", data={"csrf_token": tok, "navidrome_password": "", "trash_days": "3"})
    assert _ciphertext(sf, "navidrome_password") == first
    client.post("/settings", data={"csrf_token": tok, "navidrome_password": "other"})
    second = _ciphertext(sf, "navidrome_password")
    assert second and second != first
    page = client.get("/settings").text
    assert (
        'id="clear-navidrome_password" method="post" action="/settings/clear/navidrome_password"'
        in page
    )
    assert client.post("/settings/clear/trash_days", data={"csrf_token": tok}).status_code == 404
    r = client.post("/settings/clear/navidrome_password", data={"csrf_token": tok})
    assert r.status_code == 303
    assert _ciphertext(sf, "navidrome_password") is None
    assert saved(sf, "trash_days") == 3


def test_a_rejected_save_does_not_echo_a_secret(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    tok = login(client, "root")
    r = client.post(
        "/settings", data={"csrf_token": tok, "acoustid_app_key": SECRET, "trash_days": "abc"}
    )
    assert r.status_code == 400 and SECRET not in r.text
    assert _ciphertext(sf, "acoustid_app_key") is None  # all or nothing


def test_secret_status_from_environment_and_override_warning(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MIXSYNC_ACOUSTID_USER_KEY", "from-env-key")
    tok = login(client, "root")
    page = client.get("/settings").text
    assert "from environment" in field(page, "acoustid_user_key") and "from-env-key" not in page
    client.post("/settings", data={"csrf_token": tok, "acoustid_user_key": SECRET})
    block = field(client.get("/settings").text, "acoustid_user_key")
    assert "MIXSYNC_ACOUSTID_USER_KEY is set but ignored" in block


def test_an_unreadable_secret_is_flagged_and_the_env_value_is_used(
    client: TestClient, sf: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    tok = login(client, "root")
    client.post("/settings", data={"csrf_token": tok, "slskd_api_key": SECRET})
    with sf() as s, s.begin():
        row = s.get(AppSettings, 1)
        assert row
        row.slskd_api_key_encrypted = SecretBox(Fernet.generate_key()).encrypt(SECRET)  # other key
    monkeypatch.setenv("MIXSYNC_SLSKD_API_KEY", "env-key")
    assert load_settings().slskd_api_key == "env-key"
    page = client.get("/settings").text
    assert "Saved value unreadable, re-enter." in field(page, "slskd_api_key")
    assert "from environment" in field(page, "slskd_api_key")


def test_every_editable_field_is_in_exactly_one_group() -> None:
    grouped = [n for names in GROUPS.values() for n in names]
    assert sorted(grouped) == sorted(EDITABLE)


def _render(template: str) -> str:
    return render(
        template, albumartist="a", album="a", year=1, disc=1, track=1, title="t", ext="flac"
    )


def test_path_fields_match_what_render_fills_in() -> None:
    for name in PATH_FIELDS:
        _render(f"{{{name}}}.{{ext}}")
    with pytest.raises(KeyError):
        _render("{bogus}.{ext}")
    assert Settings().path_template  # the default validates


def test_the_first_button_saves_and_reset_changes_only_its_field(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    tok = login(client, "root")
    client.post("/settings", data={"csrf_token": tok, "trash_days": "7", "flac_level": "3"})
    page = client.get("/settings").text
    main = re.search(r'<form method="post" action="/settings">.*?</form>', page, re.S)
    assert main
    # a button with form="..." belongs to another form, so Enter never picks it
    first = re.search(r"<button(?![^>]*\sform=)[^>]*>([^<]*)", main.group(0))
    assert first and first.group(1) == "Save settings"
    assert 'id="reset-trash_days" method="post" action="/settings/reset/trash_days"' in page
    client.post("/settings/reset/trash_days", data={"csrf_token": tok})
    assert saved(sf, "trash_days") is None and saved(sf, "flac_level") == 3


def test_a_secret_keeps_its_surrounding_spaces(client: TestClient) -> None:
    tok = login(client, "root")
    client.post("/settings", data={"csrf_token": tok, "navidrome_password": " pw "})
    assert load_settings().navidrome_password == " pw "
