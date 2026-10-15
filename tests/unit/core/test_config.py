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


@pytest.mark.parametrize(
    ("drop", "other"),
    [
        ("/data", "MIXSYNC_DATA_DIR"),
        ("/downloads/drop", "MIXSYNC_DOWNLOADS_DIR"),
        ("/", "MIXSYNC_IMPORT_SOURCE_DIR"),
    ],
)
def test_drop_dir_must_not_overlap_other_dirs(
    monkeypatch: pytest.MonkeyPatch, drop: str, other: str
) -> None:
    monkeypatch.setenv("MIXSYNC_DROP_DIR", drop)
    with pytest.raises(ValidationError, match="must not overlap"):
        Settings()


def test_a_separate_drop_dir_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIXSYNC_DROP_DIR", "/drop")
    assert str(Settings().drop_dir) == "/drop"
