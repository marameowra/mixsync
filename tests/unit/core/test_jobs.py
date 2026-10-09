from datetime import timedelta
from itertools import product

import pytest

from mixsync.core.jobs import (
    TERMINAL,
    TRANSITIONS,
    IllegalTransitionError,
    JobState,
    backoff,
    transition,
)

LEGAL = {
    ("queued", "leased"),
    ("leased", "running"),
    ("running", "succeeded"),
    ("running", "retry_wait"),
    ("retry_wait", "queued"),
    ("running", "failed"),
    ("leased", "queued"),
    ("running", "queued"),
}


@pytest.mark.parametrize(("a", "b"), list(product(JobState, JobState)))
def test_transitions(a: JobState, b: JobState) -> None:
    legal = (a.value, b.value) in LEGAL or (b is JobState.CANCELLED and a not in TERMINAL)
    if legal:
        assert transition(a, b) is b
    else:
        with pytest.raises(IllegalTransitionError):
            transition(a, b)


def test_terminal_states_have_no_exits() -> None:
    assert all(not TRANSITIONS[s] for s in TERMINAL)


def test_backoff_is_exponential_and_capped() -> None:
    assert [backoff(n).total_seconds() for n in (1, 2, 3)] == [30, 60, 120]
    assert backoff(50) == timedelta(hours=1)
