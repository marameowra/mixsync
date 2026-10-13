from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes (UTC); Postgres aware ones in the session zone."""
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
