"""App settings (ADR 0021): the startup snapshot resolves saved over environment over default."""

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from mixsync.__main__ import startup_settings
from mixsync.core.config import Settings
from mixsync.db.app_settings import ENV_ONLY, Resolved, load_settings, resolve
from mixsync.db.engine import make_engine
from mixsync.db.migrate import downgrade, upgrade
from mixsync.db.models.auth import AppSettings


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def _save(settings: Settings, **values: Any) -> None:
    with Session(make_engine(settings)) as s, s.begin():
        row = s.get(AppSettings, 1) or AppSettings(id=1)
        for k, v in values.items():
            setattr(row, k, v)
        s.add(row)


def _resolved(settings: Settings) -> dict[str, Resolved]:
    with Session(make_engine(settings)) as s:
        return resolve(s)[1]


def test_nothing_saved_and_no_env_gives_the_defaults(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MIXSYNC_TRASH_DAYS", raising=False)
    upgrade()
    assert load_settings()[0] == Settings()
    _save(settings, min_auto_accept_confidence=90)  # a row with nothing saved changes nothing
    assert load_settings()[0] == Settings()
    assert _resolved(settings)["trash_days"] == Resolved(30, "default", False)


def test_env_only_gives_the_env_value(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIXSYNC_TRASH_DAYS", "7")
    upgrade()
    assert load_settings()[0].trash_days == 7
    assert _resolved(settings)["trash_days"] == Resolved(7, "environment", True)


def test_a_saved_value_beats_env_across_restarts(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MIXSYNC_TRASH_DAYS", "7")
    monkeypatch.setenv("MIXSYNC_CANONICAL_MEDIA", '["CD"]')
    upgrade()
    _save(settings, trash_days=14, canonical_media=["Vinyl", "CD"], deezer_suggestions=True)
    for _ in range(2):  # each load is a fresh engine, as after a restart
        got = load_settings()[0]
        assert (got.trash_days, got.canonical_media, got.deezer_suggestions) == (
            14,
            ["Vinyl", "CD"],
            True,
        )
    assert _resolved(settings)["trash_days"] == Resolved(14, "saved", True)
    assert _resolved(settings)["deezer_suggestions"] == Resolved(True, "saved", False)


def test_saving_the_env_value_counts_as_saved_and_clearing_falls_back(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MIXSYNC_TRASH_DAYS", "7")
    upgrade()
    _save(settings, trash_days=7)
    assert _resolved(settings)["trash_days"] == Resolved(7, "saved", True)
    _save(settings, trash_days=None)
    assert _resolved(settings)["trash_days"] == Resolved(7, "environment", True)
    monkeypatch.delenv("MIXSYNC_TRASH_DAYS")
    assert _resolved(settings)["trash_days"] == Resolved(30, "default", False)


def test_clearing_a_saved_list_stores_sql_null_and_falls_back(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MIXSYNC_CANONICAL_MEDIA", raising=False)
    upgrade()
    _save(settings, canonical_media=["Vinyl"])
    _save(settings, canonical_media=None)
    with Session(make_engine(settings)) as s:
        cleared = s.scalars(
            select(AppSettings.id).where(AppSettings.canonical_media.is_(None))
        ).all()
    assert cleared == [1]
    assert _resolved(settings)["canonical_media"] == Resolved(
        ["Digital Media", "CD"], "default", False
    )


def test_an_unmigrated_database_says_to_migrate(settings: Settings) -> None:
    with pytest.raises(RuntimeError, match="not migrated.*mixsync migrate"):
        load_settings()


def test_bootstrap_mount_and_secret_settings_stay_env_only(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MIXSYNC_DATA_DIR", "/music")
    upgrade()
    resolved = _resolved(settings)
    assert not set(resolved) & ENV_ONLY
    assert set(resolved) | ENV_ONLY == set(Settings.model_fields)  # every field is one or other
    with make_engine(settings).connect() as c:
        columns = {col["name"] for col in inspect(c).get_columns("app_settings")}
    assert not columns & ENV_ONLY  # nothing to save them in
    got = load_settings()[0]
    assert (got.data_dir, got.database_url) == ("/music", settings.database_url)


def test_an_invalid_saved_value_logs_and_falls_back(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("MIXSYNC_MB_RATE", "0.5")
    monkeypatch.delenv("MIXSYNC_TIMEZONE", raising=False)
    upgrade()
    _save(settings, mb_rate=2.0, timezone="Mars/Olympus", trash_days=9)
    with caplog.at_level(logging.ERROR):
        got = load_settings()[0]
    assert (got.mb_rate, got.timezone, got.trash_days) == (0.5, "UTC", 9)
    assert "saved setting mb_rate is invalid" in caplog.text
    assert "saved setting timezone is invalid" in caplog.text
    resolved = _resolved(settings)
    assert resolved["mb_rate"] == Resolved(0.5, "environment", True)
    assert resolved["timezone"] == Resolved("UTC", "default", False)


def test_startup_logs_at_the_saved_level(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MIXSYNC_LOG_LEVEL", "WARNING")
    upgrade()
    _save(settings, log_level="debug")  # the validator upper-cases it, as it does for env
    assert startup_settings()[0].log_level == "DEBUG"
    assert logging.getLogger().level == logging.DEBUG


def test_migration_adds_unsaved_columns_and_downgrade_keeps_the_cap(settings: Settings) -> None:
    upgrade("0028_worker_heartbeats")
    with make_engine(settings).begin() as c:
        c.execute(
            text(
                "INSERT INTO app_settings (id, min_auto_accept_confidence, created_at, updated_at)"
                " VALUES (1, 80, '2026-01-01', '2026-01-01')"
            )
        )
    upgrade()
    with make_engine(settings).connect() as c:
        row = c.execute(text("SELECT min_auto_accept_confidence, trash_days FROM app_settings"))
        assert tuple(row.one()) == (80, None)
    downgrade("0028_worker_heartbeats")
    with make_engine(settings).connect() as c:
        cols = {col["name"] for col in inspect(c).get_columns("app_settings")}
        assert c.execute(text("SELECT min_auto_accept_confidence FROM app_settings")).scalar() == 80
    assert "trash_days" not in cols
