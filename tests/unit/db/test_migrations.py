from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from mixsync.core.config import Settings
from mixsync.db.base import Base
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
    assert _version(settings) == "0009_decision_created_at_index"
    downgrade("base")
    assert _version(settings) is None
    upgrade()
    assert _version(settings) == "0009_decision_created_at_index"


def test_models_match_migrations(settings: Settings) -> None:
    import mixsync.db.models  # noqa: F401  (registers every table)

    upgrade()
    with make_engine(settings).connect() as c:
        diff = compare_metadata(MigrationContext.configure(c), Base.metadata)
    assert diff == []
