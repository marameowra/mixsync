import pytest
from pydantic import ValidationError

from mixsync.core.config import Settings


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIXSYNC_DATABASE_URL", raising=False)
    monkeypatch.delenv("MIXSYNC_CONFIG_DIR", raising=False)
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


@pytest.mark.parametrize(
    ("save", "drop"),
    [("/data/torrents", None), ("/downloads/torrents", None), ("/drop/t", "/drop"), ("/", None)],
)
def test_the_qbittorrent_dir_stays_out_of_every_scanned_or_managed_dir(
    monkeypatch: pytest.MonkeyPatch, save: str, drop: str | None
) -> None:
    """Adopt scans the downloads, the drop folder its own dir: neither ever reads a seeding
    download, and nothing under the data dir is one."""
    monkeypatch.setenv("MIXSYNC_QBITTORRENT_DIR", save)
    if drop:
        monkeypatch.setenv("MIXSYNC_DROP_DIR", drop)
    with pytest.raises(ValidationError, match="MIXSYNC_.*_DIR must not overlap"):
        Settings()
    monkeypatch.setenv("MIXSYNC_QBITTORRENT_DIR", "/torrents")
    assert str(Settings().qbittorrent_dir) == "/torrents"


def test_a_relative_qbittorrent_dir_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIXSYNC_QBITTORRENT_DIR", "torrents")  # qBittorrent would resolve it
    with pytest.raises(ValidationError, match="must be an absolute path"):
        Settings()


def test_a_separate_drop_dir_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIXSYNC_DROP_DIR", "/drop")
    assert str(Settings().drop_dir) == "/drop"


def test_adopt_downloads_default_env_and_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIXSYNC_ADOPT_DOWNLOADS", raising=False)
    monkeypatch.delenv("MIXSYNC_ADOPT_SETTLE_MINUTES", raising=False)
    s = Settings()
    assert (s.adopt_downloads, s.adopt_settle_minutes) == (False, 10)
    monkeypatch.setenv("MIXSYNC_ADOPT_DOWNLOADS", "true")
    monkeypatch.setenv("MIXSYNC_ADOPT_SETTLE_MINUTES", "3")
    s = Settings()
    assert (s.adopt_downloads, s.adopt_settle_minutes) == (True, 3)
    monkeypatch.setenv("MIXSYNC_ADOPT_SETTLE_MINUTES", "0")
    with pytest.raises(ValidationError, match="adopt_settle_minutes"):
        Settings()


@pytest.mark.parametrize(
    "url",
    ["postgresql://u:p@h/db", "postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"],
)
def test_postgres_url_gets_psycopg_driver(url: str) -> None:
    assert Settings(database_url=url).database_url == "postgresql+psycopg://u:p@h/db"


def test_sqlite_url_unchanged() -> None:
    assert Settings(database_url="sqlite:///x.db").database_url == "sqlite:///x.db"


@pytest.mark.parametrize(
    "url", ["postgresql+psycopg2://u:p@h/db", "postgresql+asyncpg://u:p@h/db", "mysql://h/db"]
)
def test_other_drivers_rejected(url: str) -> None:
    with pytest.raises(ValidationError, match=r"postgresql\+psycopg://"):
        Settings(database_url=url)


@pytest.mark.parametrize(
    ("url", "want"),
    [
        ("postgresql://u:p@h/db?sslmode=require", "postgresql+psycopg://u:p@h/db?sslmode=require"),
        ("postgresql:///db", "postgresql+psycopg:///db"),
    ],
)
def test_postgres_url_rewrite_keeps_rest(url: str, want: str) -> None:
    assert Settings(database_url=url).database_url == want


def test_rejected_url_does_not_leak_password() -> None:
    with pytest.raises(ValidationError) as e:
        Settings(database_url="postgresql+psycopg2://u:secret@h/db")
    assert "secret" not in str(e.value)
