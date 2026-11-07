import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

from mixsync.core.clock import SystemClock
from mixsync.core.config import Settings
from mixsync.db.backup import backup
from mixsync.db.engine import make_engine

log = logging.getLogger(__name__)


def _config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    return cfg


def _backup_before_upgrade() -> None:
    """An existing database that is behind head gets a backup; a failure aborts the upgrade."""
    settings = Settings()
    engine = make_engine(settings)
    try:
        with engine.connect() as c:
            current = MigrationContext.configure(c).get_current_revision()
        if current is None or current == ScriptDirectory.from_config(_config()).get_current_head():
            return
        if engine.dialect.name != "sqlite":
            log.warning("not backing up before the upgrade: use pg_dump for Postgres")
            return
        path = backup(
            engine, settings.backup_dir or Path(settings.config_dir) / "backups", SystemClock()
        )
        log.info("database backed up before the upgrade: %s", path)
    finally:
        engine.dispose()


def upgrade(revision: str = "head") -> None:
    _backup_before_upgrade()
    command.upgrade(_config(), revision)


def downgrade(revision: str = "base") -> None:
    command.downgrade(_config(), revision)
