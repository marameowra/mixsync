from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import Clock, aware
from mixsync.db.models.infra import RateBucket
from mixsync.ratelimit.services import ServiceLimits


def _lock(s: Session) -> None:
    if s.get_bind().dialect.name == "sqlite":
        # first statement of the transaction: pysqlite does not autobegin for non-DML
        s.execute(text("BEGIN IMMEDIATE"))


def _locked_row(s: Session, service: str, rate: float, now: datetime) -> RateBucket:
    """The `service` row, created if missing, locked FOR UPDATE (SQLite: by `_lock`)."""
    cap = max(1.0, rate)
    if s.get_bind().dialect.name != "sqlite":
        # FOR UPDATE locks nothing on a missing row: the loser of a first-use race no-ops
        s.execute(
            pg_insert(RateBucket)
            .values(service=service, tokens=cap, capacity=cap, refill_per_sec=rate, updated_at=now)
            .on_conflict_do_nothing()
        )
    row = s.get(RateBucket, service, with_for_update=True)
    if row is None:
        row = RateBucket(
            service=service, tokens=cap, capacity=cap, refill_per_sec=rate, updated_at=now
        )
        s.add(row)
    return row


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
        _lock(s)
        row = _locked_row(s, self.service, rate, now)
        # pick up rate changes from config; reserve() clamps tokens to the new capacity
        row.refill_per_sec, row.capacity = rate, max(1.0, rate)
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


class Slots:
    """At most `n` holders at once across every worker. Each slot is a `rate_buckets` row
    (`<name>:<i>`) whose `blocked_until` is its holder's lease, so a crashed holder's slot frees
    itself when the lease runs out."""

    def __init__(
        self, name: str, n: int, sessions: sessionmaker[Session], clock: Clock, lease: timedelta
    ) -> None:
        self.name, self.n, self.sessions, self.clock, self.lease = name, n, sessions, clock, lease

    def take(self) -> tuple[str, datetime] | None:
        """Claim a free slot: its key and lease end, or None when all `n` are held."""
        now = self.clock.now()
        with self.sessions.begin() as s:
            _lock(s)
            for i in range(self.n):  # same order in every worker: no deadlock on Postgres
                row = _locked_row(s, f"{self.name}:{i}", 0.0, now)
                if row.blocked_until is None or aware(row.blocked_until) <= now:
                    row.blocked_until, row.updated_at = now + self.lease, now
                    return row.service, now + self.lease
        return None

    def give(self, slot: tuple[str, datetime]) -> None:
        key, until = slot
        with self.sessions.begin() as s:
            _lock(s)
            row = s.get(RateBucket, key, with_for_update=True)
            # a lease that ran out may already belong to someone else: leave theirs alone
            if row is not None and row.blocked_until and aware(row.blocked_until) == until:
                row.blocked_until = None
