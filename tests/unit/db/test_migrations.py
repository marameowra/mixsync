from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from mixsync.core.config import Settings
from mixsync.db.base import Base
from mixsync.db.engine import make_engine
from mixsync.db.migrate import downgrade, upgrade


def _version(settings: Settings) -> str | None:
    with make_engine(settings).connect() as c:
        if not inspect(c).has_table("alembic_version"):
            return None
        return c.execute(text("SELECT version_num FROM alembic_version")).scalar()


def test_upgrade_downgrade_upgrade(settings: Settings) -> None:
    upgrade()
    assert _version(settings) == "0017_jobs_status_index"
    downgrade("base")
    assert _version(settings) is None
    upgrade()
    assert _version(settings) == "0017_jobs_status_index"


def test_models_match_migrations(settings: Settings) -> None:
    import mixsync.db.models  # noqa: F401  (registers every table)

    upgrade()
    with make_engine(settings).connect() as c:
        diff = compare_metadata(MigrationContext.configure(c), Base.metadata)
    assert diff == []


def test_pre_0012_file_op_reads_back_as_library(settings: Settings) -> None:
    upgrade("0011_roles_sequence")
    with make_engine(settings).begin() as c:
        c.execute(
            text(
                "INSERT INTO file_ops (batch_id, kind, src, dst, status, started_at,"
                " created_at, updated_at) VALUES ('b', 'copy', 's', 'd', 'started',"
                " '2026-01-01', '2026-01-01', '2026-01-01')"
            )
        )
    upgrade()
    with make_engine(settings).connect() as c:
        assert c.execute(text("SELECT root FROM file_ops")).scalar() == "library"
