from datetime import UTC, datetime, timedelta

from sqlalchemy import inspect

from mixsync.core.config import Settings
from mixsync.core.protocols import Journal
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.journal import SqlJournal
from mixsync.db.migrate import downgrade, upgrade


class FakeClock:
    def __init__(self) -> None:
        self.t = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        self.t += timedelta(seconds=1)
        return self.t


def _journal(settings: Settings) -> SqlJournal:
    upgrade()
    return SqlJournal(make_session_factory(make_engine(settings)), FakeClock())


def test_conforms_to_protocol(settings: Settings) -> None:
    j: Journal = _journal(settings)  # pyright checks the structural match here
    assert j.pending() == []


def test_lifecycle(settings: Settings) -> None:
    j = _journal(settings)
    a = j.begin("b1", "copy", "/s/a", "/d/a")
    b = j.begin("b1", "move", "/s/b", "/d/b")
    c = j.begin("b1", "trash", "/s/c", "/t/c")
    assert [o.id for o in j.pending()] == [a, b, c]
    assert j.pending()[0].status == "started"
    j.complete(a, "hash")
    j.fail(b, "boom")
    j.rolled_back(c)
    assert j.pending() == []


def test_pending_survives_crash(settings: Settings) -> None:
    j = _journal(settings)
    done = j.begin("b", "copy", "s1", "d1")
    j.complete(done, "h")
    op_id = j.begin("b", "copy", "s2", "d2")
    # simulate a restart: brand-new engine and sessions on the same file
    j2 = SqlJournal(make_session_factory(make_engine(settings)), FakeClock())
    assert [(o.id, o.src, o.dst) for o in j2.pending()] == [(op_id, "s2", "d2")]


def test_migration_downgrade(settings: Settings) -> None:
    upgrade()
    assert {"file_ops", "tag_snapshots"} <= set(inspect(make_engine(settings)).get_table_names())
    downgrade("0001_initial")
    assert "file_ops" not in inspect(make_engine(settings)).get_table_names()
