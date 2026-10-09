from sqlalchemy import text

from mixsync.core.config import Settings
from mixsync.db.engine import make_engine
from mixsync.db.migrate import downgrade, upgrade


def _version(settings: Settings) -> str | None:
    with make_engine(settings).connect() as c:
        has_table = c.execute(text("SELECT 1 FROM sqlite_master WHERE name='alembic_version'"))
        if has_table.first() is None:
            return None
        return c.execute(text("SELECT version_num FROM alembic_version")).scalar()


def test_upgrade_downgrade_upgrade(settings: Settings) -> None:
    upgrade()
    assert _version(settings) == "0002_journal"
    downgrade("base")
    assert _version(settings) is None
    upgrade()
    assert _version(settings) == "0002_journal"
