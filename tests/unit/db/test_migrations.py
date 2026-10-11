from sqlalchemy import inspect, text

from mixsync.core.config import Settings
from mixsync.db.engine import make_engine
from mixsync.db.migrate import downgrade, upgrade


def _version(settings: Settings) -> str | None:
    with make_engine(settings).connect() as c:
        if not inspect(c).has_table("alembic_version"):
            return None
        return c.execute(text("SELECT version_num FROM alembic_version")).scalar()


def test_upgrade_downgrade_upgrade(settings: Settings) -> None:
    upgrade()
    assert _version(settings) == "0008_migrations"
    downgrade("base")
    assert _version(settings) is None
    upgrade()
    assert _version(settings) == "0008_migrations"
