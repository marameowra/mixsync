from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import Clock, aware
from mixsync.db.models.infra import RateBucket
from mixsync.ratelimit.services import ServiceLimits


class Bucket:
    """Token bucket persisted in `rate_buckets`, so every worker shares one budget.

    Each read-modify-write runs under a write lock (BEGIN IMMEDIATE on SQLite, row
    FOR UPDATE on Postgres), so concurrent processes never overspend. Callers sleep
    outside the transaction.
    """

    def __init__(
        self, service: str, lim: ServiceLimits, sessions: sessionmaker[Session], clock: Clock
    ) -> None:
        self.service = service
        self.rate = lim.rate
        self.sessions = sessions
        self.clock = clock

    def _row(self, s: Session, rate: float, now: datetime) -> RateBucket:
        if s.get_bind().dialect.name == "sqlite":
            # first statement of the transaction: pysqlite does not autobegin for non-DML
            s.execute(text("BEGIN IMMEDIATE"))
        cap = max(1.0, rate)
        if s.get_bind().dialect.name != "sqlite":
            # FOR UPDATE locks nothing on a missing row: the loser of a first-use race no-ops
            s.execute(
                pg_insert(RateBucket)
                .values(
                    service=self.service,
                    tokens=cap,
                    capacity=cap,
                    refill_per_sec=rate,
                    updated_at=now,
                )
                .on_conflict_do_nothing()
            )
        row = s.get(RateBucket, self.service, with_for_update=True)
        if row is None:
            row = RateBucket(
                service=self.service,
                tokens=cap,
                capacity=cap,
                refill_per_sec=rate,
                updated_at=now,
            )
            s.add(row)
        else:  # pick up rate changes from config; reserve() clamps tokens to the new capacity
            row.refill_per_sec = rate
            row.capacity = cap
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
