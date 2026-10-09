from pathlib import Path

from alembic import command
from alembic.config import Config


def _config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    return cfg


def upgrade(revision: str = "head") -> None:
    command.upgrade(_config(), revision)


def downgrade(revision: str = "base") -> None:
    command.downgrade(_config(), revision)
