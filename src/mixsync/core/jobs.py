from datetime import timedelta
from enum import StrEnum


class JobKind(StrEnum):
    NOOP = "noop"


class JobState(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


S = JobState
TERMINAL = frozenset({S.SUCCEEDED, S.FAILED, S.CANCELLED})
# Expired leases (leased/running) return to queued; that is the reaper's transition.
TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    S.QUEUED: frozenset({S.LEASED, S.CANCELLED}),
    S.LEASED: frozenset({S.RUNNING, S.QUEUED, S.CANCELLED}),
    S.RUNNING: frozenset({S.SUCCEEDED, S.RETRY_WAIT, S.FAILED, S.QUEUED, S.CANCELLED}),
    S.RETRY_WAIT: frozenset({S.QUEUED, S.CANCELLED}),
    S.SUCCEEDED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}

DEFAULT_LEASE = timedelta(seconds=60)


class IllegalTransitionError(ValueError):
    pass


def transition(current: JobState, new: JobState) -> JobState:
    """Return `new` if current -> new is legal, else raise."""
    if new not in TRANSITIONS[current]:
        raise IllegalTransitionError(f"illegal job transition {current} -> {new}")
    return new


def backoff(
    attempts: int, base: timedelta = timedelta(seconds=30), cap: timedelta = timedelta(hours=1)
) -> timedelta:
    """Delay before retrying after `attempts` failed attempts (>= 1): exponential, capped."""
    return min(cap, base * 2 ** min(max(attempts, 1) - 1, 30))  # clamp: avoid overflow
