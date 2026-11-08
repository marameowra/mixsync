"""App settings (ADR 0021): each process resolves one snapshot at startup, saved over
environment over default. The environment is read here and never written to the database."""

import logging
import os
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from mixsync.core.config import Settings
from mixsync.db.engine import make_engine
from mixsync.db.models.auth import AppSettings
from mixsync.db.secretbox import load_box

log = logging.getLogger(__name__)

# bootstrap and mount settings stay env-only
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
    }
)
# saved encrypted in `<name>_encrypted` (ADR 0010); write-only on the settings page
SECRETS = ("slskd_api_key", "navidrome_password", "acoustid_app_key", "acoustid_user_key")
EDITABLE = tuple(n for n in Settings.model_fields if n not in ENV_ONLY and n not in SECRETS)

Source = Literal["saved", "environment", "default"]


@dataclass(frozen=True)
class Resolved:
    value: Any
    source: Source
    env_set: bool  # MIXSYNC_<NAME> is set, whether or not a saved value overrides it
    unreadable: bool = False  # a saved secret that this key can't decrypt


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
    unreadable: set[str] = set()
    box = None  # built once, only if a ciphertext exists
    for name in SECRETS:
        token = getattr(row, f"{name}_encrypted") if row else None
        if token is None:
            continue
        box = box or load_box(Settings())
        if (plain := box.decrypt(token)) is None:
            log.error("saved setting %s cannot be decrypted with this key; ignoring it", name)
            unreadable.add(name)
        else:
            saved[name] = plain
    settings = Settings(**saved)
    env = {k.upper() for k in os.environ}
    out: dict[str, Resolved] = {}
    for name in (*EDITABLE, *SECRETS):
        env_set = f"MIXSYNC_{name.upper()}" in env
        source: Source = "saved" if name in saved else "environment" if env_set else "default"
        out[name] = Resolved(getattr(settings, name), source, env_set, name in unreadable)
    return settings, out


def load_settings() -> Settings:
    """The startup snapshot for web, worker and `all`. An invalid environment fails here."""
    engine = make_engine(Settings())
    try:
        with Session(engine) as s:
            return resolve(s)[0]
    except (OperationalError, ProgrammingError) as e:  # usually app_settings table/column missing
        raise RuntimeError(
            "could not read app settings: the database is not migrated (or not reachable);"
            " run `mixsync migrate`"
        ) from e
    finally:
        engine.dispose()
