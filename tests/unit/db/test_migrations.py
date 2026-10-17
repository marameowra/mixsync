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
    assert _version(settings) == "0018_request_lifecycle"
    downgrade("base")
    assert _version(settings) is None
    upgrade()
    assert _version(settings) == "0018_request_lifecycle"


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


def test_0018_backfills_dead_end_and_partial_from_last_error(settings: Settings) -> None:
    upgrade("0017_jobs_status_index")
    rows = [  # (provisional, status, last_error)
        (1, "failed", "none of the wanted recordings are on this release"),
        (1, "failed", "none of the wanted recordings are on the downloaded release"),
        (1, "imported", "1 of 2 wanted recordings not on this release"),
        (1, "imported", None),
        (0, "failed", "none of the wanted recordings are on this release"),
        (0, "partial", "partial: 2 of 5 tracks present; rejected after download"),
        (0, "partial", "partial"),
    ]
    with make_engine(settings).begin() as c:
        c.execute(
            text(
                "INSERT INTO users (id, username, password_hash, status, created_at, updated_at)"
                " VALUES (1, 'u', 'x', 'active', '2026-01-01', '2026-01-01')"
            )
        )
        for i, (prov, status, err) in enumerate(rows, 1):
            c.execute(
                text(
                    "INSERT INTO requests (id, user_id, release_mbid, artist, album, status,"
                    " tried, no_results_attempts, provisional, last_error, created_at,"
                    " updated_at) VALUES (:i, 1, 'm', '', '', :s, '[]', 0, :p, :e,"
                    " '2026-01-01', '2026-01-01')"
                ),
                {"i": i, "s": status, "p": prov, "e": err},
            )
    upgrade()
    with make_engine(settings).connect() as c:
        got = c.execute(text("SELECT dead_end, partial_note FROM requests ORDER BY id")).all()
    assert [bool(d) for d, _ in got] == [True, True, True, False, False, False, False]
    assert [n for _, n in got] == [None] * 5 + ["2 of 5 tracks present", None]
