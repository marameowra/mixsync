from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from mixsync.core.config import Settings
from mixsync.core.matching import Band, MatchResult, Penalty
from mixsync.db import decisions
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade

MBID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def s(settings: Settings) -> Iterator[Session]:
    upgrade()
    with make_session_factory(make_engine(settings))() as session, session.begin():
        create_user(session, "alice", "pw")
        create_user(session, "bob", "pw")
        yield session


def _result(band: Band) -> MatchResult:
    return MatchResult(0.2, band, (Penalty("title", 0.5, 2.0, "why"),), ("veto",), 1)


def _rec(s: Session, band: Band, user_id: int = 1) -> int:
    row = decisions.record(
        s, _result(band), evidence={"x": 1}, user_id=user_id, stage=2, release_mbid=MBID
    )
    return row.id


@pytest.mark.parametrize(
    ("band", "status"),
    [
        (Band.AUTO_ACCEPT, "auto_accepted"),
        (Band.REVIEW, "pending_review"),
        (Band.REJECT, "rejected_auto"),
    ],
)
def test_record_status_from_band(s: Session, band: Band, status: str) -> None:
    row = decisions.get(s, _rec(s, band))
    assert row is not None and row.status == status
    assert row.breakdown == [{"key": "title", "penalty": 0.5, "weight": 2.0, "reason": "why"}]
    assert row.vetoes == ["veto"] and row.scorer_version == 1


def test_pending_for(s: Session) -> None:
    a, b = _rec(s, Band.REVIEW, 1), _rec(s, Band.REVIEW, 2)
    _rec(s, Band.AUTO_ACCEPT, 1)
    assert [d.id for d in decisions.pending_for(s, 1)] == [a]
    assert [d.id for d in decisions.pending_for(s, None)] == [a, b]


def test_act_once_only(s: Session) -> None:
    i = _rec(s, Band.REVIEW)
    decisions.act(s, i, 2, "repick", MBID)
    row = decisions.get(s, i)
    assert row is not None
    assert (row.status, row.final_action, row.chosen_release_mbid, row.acted_by) == (
        "repick",
        "repick",
        MBID,
        2,
    )
    assert row.acted_at is not None
    with pytest.raises(decisions.DecisionStateError):
        decisions.act(s, i, 1, "accept")
    assert row.status == "repick"


def test_act_rejects_non_pending_and_bad_args(s: Session) -> None:
    with pytest.raises(decisions.DecisionStateError):
        decisions.act(s, _rec(s, Band.AUTO_ACCEPT), 1, "reject")
    i = _rec(s, Band.REVIEW)
    for action, mbid in [("bogus", None), ("repick", None), ("accept", MBID)]:
        with pytest.raises(ValueError):
            decisions.act(s, i, 1, action, mbid)
