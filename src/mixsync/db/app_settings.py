"""App settings (ADR 0021): each process resolves one snapshot at startup, saved over
environment over default. The environment is read here and never written to the database."""

import logging
import os
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy.orm import Session

from mixsync.core.config import Settings
from mixsync.db.engine import make_engine
from mixsync.db.models.auth import AppSettings

log = logging.getLogger(__name__)

# bootstrap and mount settings stay env-only; the secrets get their own saved columns in #346
ENV_ONLY = frozenset(
    {
        "database_url",
        "config_dir",
        "secret_key",
        "data_dir",
        "downloads_dir",
        "drop_dir",
        "import_source_dir",
        "backup_dir",
        "slskd_api_key",
        "navidrome_password",
        "acoustid_app_key",
        "acoustid_user_key",
    }
)
EDITABLE = tuple(name for name in Settings.model_fields if name not in ENV_ONLY)

Source = Literal["saved", "environment", "default"]


@dataclass(frozen=True)
class Resolved:
    value: Any
    source: Source
    env_set: bool  # MIXSYNC_<NAME> is set, whether or not a saved value overrides it


def resolve(s: Session) -> tuple[Settings, dict[str, Resolved]]:
    """The effective settings, and per app setting its value, source and whether env sets it.
    A saved value that fails today's validators is logged and skipped, never fatal."""
    row = s.get(AppSettings, 1)
    saved: dict[str, Any] = {}
    for name in EDITABLE:
        value = getattr(row, name) if row else None
        if value is None:
            continue
        try:
            Settings(**{name: value})  # constructor values beat the environment
        except ValidationError as e:
            log.error("saved setting %s is invalid; using the environment or default: %s", name, e)
            continue
        saved[name] = value
    settings = Settings(**saved)
    env = {k.upper() for k in os.environ}
    out: dict[str, Resolved] = {}
    for name in EDITABLE:
        env_set = f"MIXSYNC_{name.upper()}" in env
        source: Source = "saved" if name in saved else "environment" if env_set else "default"
        out[name] = Resolved(getattr(settings, name), source, env_set)
    return settings, out


def load_settings() -> Settings:
    """The startup snapshot for web, worker and `all`. An invalid environment fails here."""
    engine = make_engine(Settings())
    try:
        with Session(engine) as s:
            return resolve(s)[0]
    finally:
        engine.dispose()
