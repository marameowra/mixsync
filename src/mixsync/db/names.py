from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import Clock
from mixsync.db.models.infra import MbName


def put_names(s: Session, now: datetime, rows: list[tuple[str, str, str]]) -> None:
    """Upsert in the caller's session (get-then-set works on SQLite and Postgres)."""
    for kind, mbid, name in rows:
        if row := s.get(MbName, (kind, mbid)):
            row.name, row.updated_at = name, now
        else:
            s.add(MbName(kind=kind, mbid=mbid, name=name, updated_at=now))


def stored_names(s: Session, mbids: set[str]) -> dict[str, str]:
    """mbid -> name for every kind; ids are UUIDs, so they do not clash across kinds."""
    return dict(s.execute(select(MbName.mbid, MbName.name).where(MbName.mbid.in_(mbids))).all())


class SqlNameStore:
    """NameStore over mb_name. Every call commits in its own short session."""

    def __init__(self, sessions: sessionmaker[Session], clock: Clock) -> None:
        self._sessions = sessions
        self._clock = clock

    def save(self, rows: list[tuple[str, str, str]]) -> None:
        with self._sessions.begin() as s:
            put_names(s, self._clock.now(), rows)
