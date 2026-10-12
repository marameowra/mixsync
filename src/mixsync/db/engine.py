from sqlite3 import Connection
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

from mixsync.core.config import Settings

_PRAGMAS = (
    "journal_mode=WAL",
    "foreign_keys=ON",
    "busy_timeout=5000",
    "synchronous=NORMAL",
)


def make_engine(settings: Settings | None = None) -> Engine:
    engine = create_engine(
        (settings or Settings()).database_url,
        hide_parameters=True,  # a failed statement must not put bound secrets in its traceback
    )
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _set_pragmas(dbapi_conn: Connection, _record: ConnectionPoolEntry) -> None:  # pyright: ignore[reportUnusedFunction]  # registered by decorator
            cur = dbapi_conn.cursor()
            for p in _PRAGMAS:
                cur.execute(f"PRAGMA {p}")
            cur.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def user_sessions(factory: sessionmaker[Session]) -> sessionmaker[Session]:
    """The web app's sessions. Jobs they queue are user-initiated, so they run first."""
    kw: dict[str, Any] = {**factory.kw, "info": {"user": True}}
    return sessionmaker(**kw)
