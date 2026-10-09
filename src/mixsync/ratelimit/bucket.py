from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import Clock
from mixsync.db.models.infra import RateBucket
from mixsync.ratelimit.services import ServiceLimits


def aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; everything stored is UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class Bucket:
    """Token bucket persisted in `rate_buckets`, so every worker shares one budget.

    shortcut: atomic per process (no await inside a transaction), not across SQLite
    processes; upgrade to SELECT ... FOR UPDATE / BEGIN IMMEDIATE if contention shows up.
    """

    def __init__(
        self, service: str, lim: ServiceLimits, sessions: sessionmaker[Session], clock: Clock
    ) -> None:
        self.service = service
        self.rate = lim.rate
        self.sessions = sessions
        self.clock = clock

    def _row(self, s: Session, rate: float, now: datetime) -> RateBucket:
        row = s.get(RateBucket, self.service)
        if row is None:
            cap = max(1.0, rate)
            row = RateBucket(
                service=self.service,
                tokens=cap,
                capacity=cap,
                refill_per_sec=rate,
                updated_at=now,
            )
            s.add(row)
        return row

    def reserve(self) -> timedelta:
        """Take a token if available (returns 0), else return how long to wait."""
        if self.rate is None:
            return timedelta(0)
        now = self.clock.now()
        with self.sessions.begin() as s:
            row = self._row(s, self.rate, now)
            elapsed = (now - aware(row.updated_at)).total_seconds()
            row.tokens = min(row.capacity, row.tokens + max(0.0, elapsed) * row.refill_per_sec)
            row.updated_at = now
            if row.blocked_until is not None and aware(row.blocked_until) > now:
                return aware(row.blocked_until) - now
            if row.tokens >= 1:
                row.tokens -= 1
                return timedelta(0)
            return timedelta(seconds=(1 - row.tokens) / row.refill_per_sec)

    def block(self, delay: timedelta) -> None:
        """Pause every worker for `delay` (Retry-After or backoff)."""
        if self.rate is None:
            return
        now = self.clock.now()
        with self.sessions.begin() as s:
            row = self._row(s, self.rate, now)
            row.blocked_until = max(now + delay, aware(row.blocked_until or now))
