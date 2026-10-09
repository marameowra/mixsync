from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from mixsync.core.matching import Band, MatchResult
from mixsync.db.models.matching import MatchDecision

PENDING = "pending_review"
_STATUS_BY_BAND = {
    Band.AUTO_ACCEPT: "auto_accepted",
    Band.REVIEW: PENDING,
    Band.REJECT: "rejected_auto",
}
_STATUS_BY_ACTION = {"accept": "accepted", "reject": "rejected", "repick": "repick"}


class DecisionStateError(Exception):
    """The decision is not pending review (already acted on, or never needed review)."""


def record(
    s: Session,
    result: MatchResult,
    *,
    evidence: dict[str, Any],
    user_id: int,
    stage: int,
    release_mbid: str,
) -> MatchDecision:
    row = MatchDecision(
        user_id=user_id,
        stage=stage,
        release_mbid=release_mbid,
        band=result.band.value,
        distance=result.distance,
        breakdown=[asdict(p) for p in result.breakdown],
        vetoes=list(result.vetoes),
        scorer_version=result.scorer_version,
        evidence=evidence,
        status=_STATUS_BY_BAND[result.band],
    )
    s.add(row)
    s.flush()
    return row


def pending_for(s: Session, user_id: int | None) -> list[MatchDecision]:
    q = select(MatchDecision).where(MatchDecision.status == PENDING).order_by(MatchDecision.id)
    if user_id is not None:
        q = q.where(MatchDecision.user_id == user_id)
    return list(s.scalars(q))


def get(s: Session, decision_id: int) -> MatchDecision | None:
    return s.get(MatchDecision, decision_id)


def act(
    s: Session,
    decision_id: int,
    actor_id: int,
    action: str,
    chosen_release_mbid: str | None = None,
) -> None:
    if action not in _STATUS_BY_ACTION:
        raise ValueError(f"unknown action: {action}")
    if (action == "repick") != (chosen_release_mbid is not None):
        raise ValueError("chosen_release_mbid is for repick, and repick requires it")
    # Conditional UPDATE so two concurrent submits cannot both win.
    done = s.execute(
        update(MatchDecision)
        .where(MatchDecision.id == decision_id, MatchDecision.status == PENDING)
        .values(
            status=_STATUS_BY_ACTION[action],
            final_action=action,
            chosen_release_mbid=chosen_release_mbid,
            acted_by=actor_id,
            acted_at=datetime.now(UTC),
        )
    )
    if done.rowcount == 0:  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType]
        raise DecisionStateError(f"decision {decision_id} is not pending review")
    s.expire_all()
