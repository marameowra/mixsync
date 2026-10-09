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
