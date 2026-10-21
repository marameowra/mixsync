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
    assert _version(settings) == "0022_request_content_key"
    downgrade("base")
    assert _version(settings) is None
    upgrade()
    assert _version(settings) == "0022_request_content_key"


def test_models_match_migrations(settings: Settings) -> None:
    import mixsync.db.models  # noqa: F401  (registers every table)

    upgrade()
    with make_engine(settings).connect() as c:
        diff = compare_metadata(MigrationContext.configure(c), Base.metadata)
    assert diff == []


def test_decision_release_given_is_backfilled_from_the_last_verify(settings: Settings) -> None:
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
                    f" 'review', '[]', 0, 'manual', :prov, '[]', :m, {day}, {day})"
                ),
                {"m": migration, "prov": False},
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
        for did, rid in ((1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (6, None)):
            c.execute(
                text(
                    "INSERT INTO match_decisions (id, user_id, stage, release_mbid, band, distance,"
                    " breakdown, vetoes, scorer_version, evidence, status, created_at, updated_at)"
                    " VALUES (:i, 1, 2, '', 'review', 0.5, '[]', '[]', 1, :e, 'pending_review',"
                    f" {day}, {day})"
                ),
                {"i": did, "e": json.dumps({} if rid is None else {"request_id": rid})},
            )
    upgrade()
    with make_engine(settings).connect() as c:
        got = dict(c.execute(text("SELECT id, release_given FROM match_decisions")).all())
    # the request's last VERIFY decides; with none, only a migration's request was identified;
    # a decision with no request keeps the default
    assert {k: bool(v) for k, v in got.items()} == {
        1: True,
        2: False,
        3: True,
        4: False,
        5: False,
        6: True,
    }


def test_request_origin_is_backfilled_from_the_first_verify(settings: Settings) -> None:
    upgrade("0019_decision_release_given")
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
        # (id, source, migration): migration, drop, adopt, searched, from-files with no VERIFY
        for rid, source, migration in (
            (1, "manual", 7),
            (2, "drop", None),
            (3, "adopt", None),
            (4, "manual", None),
            (5, "drop", None),
        ):
            c.execute(
                text(
                    "INSERT INTO requests (id, user_id, release_mbid, artist, album, status,"
                    " tried, no_results_attempts, source, provisional, recording_mbids,"
                    f" migration_id, created_at, updated_at) VALUES ({rid}, 1, '', '', '',"
                    f" 'review', '[]', 0, :s, :prov, '[]', :m, {day}, {day})"
                ),
                {"s": source, "m": migration, "prov": False},
            )
        for rid, folder in ((1, "first"), (1, "second"), (2, "d"), (3, "a"), (4, "s")):
            c.execute(
                text(
                    "INSERT INTO jobs (kind, payload, status, priority, attempts, max_attempts,"
                    f" run_after, created_at, updated_at) VALUES ('verify', :p, 'succeeded', 0, 1,"
                    f" 1, {day}, {day}, {day})"
                ),
                {
                    "p": json.dumps(
                        {
                            "request_id": rid,
                            "release_mbid": None,
                            "candidate": {"folder": folder},
                            "paths": [f"/{folder}/1.mp3", f"/{folder}/2.mp3"],
                        }
                    )
                },
            )
    upgrade()
    with make_engine(settings).connect() as c:
        got = dict(c.execute(text("SELECT id, origin FROM requests")).all())
    got = {k: json.loads(v) if isinstance(v, str) else v for k, v in got.items()}
    assert got == {
        1: {"candidate": {"folder": "first"}, "paths": ["/first/1.mp3", "/first/2.mp3"]},
        2: {"candidate": {"folder": "d"}, "paths": ["/d/1.mp3", "/d/2.mp3"]},
        3: {"candidate": {"folder": "a"}, "paths": ["/a/1.mp3", "/a/2.mp3"]},
        4: None,
        5: None,
    }
    downgrade("0019_decision_release_given")
    with make_engine(settings).connect() as c:
        cols = [col["name"] for col in inspect(c).get_columns("requests")]
    assert "origin" not in cols


def test_request_content_key_is_backfilled_from_verify_idempotency_keys(
    settings: Settings,
) -> None:
    upgrade("0020_request_origin")
    day = "'2026-01-01'"
    with make_engine(settings).begin() as c:
        c.execute(
            text(
                "INSERT INTO users (id, username, password_hash, status, created_at, updated_at)"
                f" VALUES (1, 'u', 'x', 'active', {day}, {day})"
            )
        )
        # (id, source): drop retried, adopt, searched, drop with no VERIFY job
        for rid, source in ((1, "drop"), (2, "adopt"), (3, "manual"), (4, "drop")):
            c.execute(
                text(
                    "INSERT INTO requests (id, user_id, release_mbid, artist, album, status,"
                    " tried, no_results_attempts, source, provisional, recording_mbids,"
                    f" created_at, updated_at) VALUES ({rid}, 1, '', '', '', 'review', '[]', 0,"
                    f" :s, :prov, '[]', {day}, {day})"
                ),
                {"s": source, "prov": False},
            )
        for rid, key in ((1, "drop:abc"), (1, "drop:abc:2"), (2, "adopt:def"), (3, "other:x")):
            c.execute(
                text(
                    "INSERT INTO jobs (kind, payload, status, priority, attempts, max_attempts,"
                    " idempotency_key, run_after, created_at, updated_at) VALUES ('verify', :p,"
                    f" 'succeeded', 0, 1, 1, :k, {day}, {day}, {day})"
                ),
                {"p": json.dumps({"request_id": rid}), "k": key},
            )
    upgrade()
    with make_engine(settings).connect() as c:
        got = dict(c.execute(text("SELECT id, content_key FROM requests")).all())
    assert got == {1: "drop:abc", 2: "adopt:def", 3: None, 4: None}
    downgrade("0021_request_budget")
    with make_engine(settings).connect() as c:
        cols = [col["name"] for col in inspect(c).get_columns("requests")]
    assert "content_key" not in cols


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
