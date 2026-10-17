import json

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
    assert _version(settings) == "0019_request_release_given"
    downgrade("base")
    assert _version(settings) is None
    upgrade()
    assert _version(settings) == "0019_request_release_given"


def test_models_match_migrations(settings: Settings) -> None:
    import mixsync.db.models  # noqa: F401  (registers every table)

    upgrade()
    with make_engine(settings).connect() as c:
        diff = compare_metadata(MigrationContext.configure(c), Base.metadata)
    assert diff == []


def test_release_given_is_backfilled_from_the_last_verify(settings: Settings) -> None:
    upgrade("0018_request_lifecycle")
    day = "'2026-01-01'"
    with make_engine(settings).begin() as c:
        c.execute(
            text(
                "INSERT INTO users (id, username, password_hash, status, created_at, updated_at)"
                f" VALUES (1, 'u', 'x', 'active', {day}, {day})"
            )
        )
        c.execute(
            text(
                "INSERT INTO migrations (id, user_id, status, summary, manifest_path, created_at,"
                f" updated_at) VALUES (7, 1, 'running', '{{}}', '', {day}, {day})"
            )
        )
        for rid, migration in ((1, None), (2, None), (3, None), (4, 7), (5, 7)):
            c.execute(
                text(
                    "INSERT INTO requests (id, user_id, release_mbid, artist, album, status,"
                    " tried, no_results_attempts, source, provisional, recording_mbids,"
                    f" migration_id, created_at, updated_at) VALUES ({rid}, 1, '', '', '',"
                    f" 'review', '[]', 0, 'manual', 0, '[]', :m, {day}, {day})"
                ),
                {"m": migration},
            )
        for rid, release in ((1, "rel"), (2, "rel"), (2, None), (3, None), (3, "rel"), (4, None)):
            c.execute(
                text(
                    "INSERT INTO jobs (kind, payload, status, priority, attempts, max_attempts,"
                    f" run_after, created_at, updated_at) VALUES ('verify', :p, 'succeeded', 0, 1,"
                    f" 1, {day}, {day}, {day})"
                ),
                {"p": json.dumps({"request_id": rid, "release_mbid": release})},
            )
    upgrade()
    with make_engine(settings).connect() as c:
        got = dict(c.execute(text("SELECT id, release_given FROM requests")).all())
    # the last VERIFY decides; with none, only a migration's request was identified
    assert {k: bool(v) for k, v in got.items()} == {1: True, 2: False, 3: True, 4: False, 5: False}


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
        (True, "failed", "none of the wanted recordings are on this release"),
        (True, "failed", "none of the wanted recordings are on the downloaded release"),
        (True, "imported", "1 of 2 wanted recordings not on this release"),
        (True, "imported", None),
        (False, "failed", "none of the wanted recordings are on this release"),
        (False, "partial", "partial: 2 of 5 tracks present; rejected after download"),
        (False, "downloading", "partial: 1 of 3 tracks present"),
        (False, "partial", "partial"),
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
    assert [bool(d) for d, _ in got] == [True, True, True, False, False, False, False, False]
    assert [n for _, n in got] == [None] * 5 + [
        "2 of 5 tracks present",
        "1 of 3 tracks present",
        None,
    ]
