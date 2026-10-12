import pytest
from pydantic import ValidationError

from mixsync.core.config import Settings


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIXSYNC_DATABASE_URL", raising=False)
    s = Settings()
    assert s.database_url == "sqlite:////config/mixsync.db"
    assert s.config_dir == "/config"


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIXSYNC_CONFIG_DIR", "/x")
    assert Settings().config_dir == "/x"


def test_bad_url_fails_readably(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIXSYNC_DATABASE_URL", "mysql://nope")
    with pytest.raises(ValidationError, match="must start with sqlite"):
        Settings()


def test_unknown_timezone_fails_readably(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIXSYNC_TIMEZONE", "Mars/Olympus")
    with pytest.raises(ValidationError, match="not a known time zone"):
        Settings()


def test_trash_days_default_env_and_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIXSYNC_TRASH_DAYS", raising=False)
    assert Settings().trash_days == 30
    monkeypatch.setenv("MIXSYNC_TRASH_DAYS", "7")
    assert Settings().trash_days == 7
    monkeypatch.setenv("MIXSYNC_TRASH_DAYS", "0")
    with pytest.raises(ValidationError, match="trash_days"):
        Settings()
