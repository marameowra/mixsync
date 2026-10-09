from sqlalchemy import text

from mixsync.core.config import Settings
from mixsync.db.engine import make_engine


def test_pragmas_applied(settings: Settings) -> None:
    with make_engine(settings).connect() as c:

        def pragma(name: str) -> object:
            return c.execute(text(f"PRAGMA {name}")).scalar_one()

        assert pragma("journal_mode") == "wal"
        assert pragma("foreign_keys") == 1
        assert pragma("busy_timeout") == 5000
        assert pragma("synchronous") == 1  # NORMAL
