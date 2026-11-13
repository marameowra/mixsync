"""App settings (ADR 0021): each process resolves one snapshot at startup, saved over
environment over default. The environment is read here and never written to the database."""

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import ColumnElement, select, update
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from mixsync.core.clock import aware
from mixsync.core.config import MEDIA_SERVER_KINDS, Settings, env_names
from mixsync.core.jobs import WORKER_STALE_AFTER
from mixsync.db.engine import make_engine
from mixsync.db.models.accounts import UserLink
from mixsync.db.models.auth import AppSettings, User
from mixsync.db.models.discovery import (
    DiscoveryPlaylist,
    MirroredPlaylist,
    ProvisionalTrack,
    SmartPlaylist,
)
from mixsync.db.models.infra import WorkerHeartbeat
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
        "qbittorrent_dir",
        "sabnzbd_dir",
    }
)
# saved encrypted in `<name>_encrypted` (ADR 0010); write-only on the settings page
SECRETS = (
    "slskd_api_key",
    "media_server_password",
    "acoustid_app_key",
    "acoustid_user_key",
    "prowlarr_api_key",
    "qbittorrent_password",
    "sabnzbd_api_key",
)
EDITABLE = tuple(n for n in Settings.model_fields if n not in ENV_ONLY and n not in SECRETS)

Source = Literal["saved", "environment", "default"]


@dataclass(frozen=True)
class Resolved:
    value: Any
    source: Source
    env_set: bool  # its variable or old alias is set, whether or not a saved value overrides it
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
        env_set = any(n in env for n in env_names(name))
        source: Source = "saved" if name in saved else "environment" if env_set else "default"
        out[name] = Resolved(getattr(settings, name), source, env_set, name in unreadable)
    return settings, out


def load_settings() -> tuple[Settings, datetime | None]:
    """The startup snapshot for web, worker and `all`, and its settings version: the saved
    row's `settings_saved_at` (None if no app setting was ever saved). An invalid environment
    fails here."""
    engine = make_engine(Settings())
    try:
        with Session(engine) as s:
            row = s.get(AppSettings, 1)
            return resolve(s)[0], row.settings_saved_at if row else None
    except (OperationalError, ProgrammingError) as e:  # usually app_settings table/column missing
        raise RuntimeError(
            "could not read app settings: the database is not migrated (or not reachable);"
            " run `mixsync migrate`"
        ) from e
    finally:
        engine.dispose()


def _server_link() -> ColumnElement[bool]:
    return UserLink.service.in_(list(MEDIA_SERVER_KINDS))


def server_link_users(s: Session) -> list[str]:
    """Usernames with a media server link: the users a switch of server makes relink."""
    q = select(User.username).join(UserLink, UserLink.user_id == User.id)
    return list(s.scalars(q.where(_server_link()).order_by(User.username)))


def switch_media_server(s: Session, kind: str) -> None:
    """ADR 0024: stored ids belong to the old server, so clear them (the next push creates
    playlists again and tracks are looked up again), and every media server link must be made
    again. Nothing on disk changes."""
    for model in (DiscoveryPlaylist, MirroredPlaylist, SmartPlaylist):
        s.execute(update(model).values(server_playlist_id=None))
    s.execute(update(ProvisionalTrack).values(server_song_id=None))
    # an empty secret never decrypts, so link_secret keeps needs_relink set until a relink
    s.execute(
        update(UserLink)
        .where(_server_link())
        .values(service=kind, secret_encrypted="", needs_relink=True)
    )


def restart_pending(s: Session, web_version: datetime | None, now: datetime) -> list[str]:
    """The processes still running settings older than the saved ones: "web", "worker", or the
    worker as unknown when no worker heartbeat is recent. Empty while nothing was ever saved."""
    row = s.get(AppSettings, 1)
    if row is None or row.settings_saved_at is None:
        return []
    saved = aware(row.settings_saved_at)

    def behind(version: datetime | None) -> bool:
        return version is None or aware(version) < saved

    out = ["web"] if behind(web_version) else []
    # the newest heartbeat, as worker_age reads it: a just-restarted worker beats its old row
    beat = s.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.seen_at.desc())).first()
    if beat is None or now - aware(beat.seen_at) > WORKER_STALE_AFTER:
        out.append("worker (unknown: no recent heartbeat)")
    elif behind(beat.settings_version):
        out.append("worker")
    return out
