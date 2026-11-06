"""Which ops of a plan are done: a `plan_progress` row if there is one, else the op's own
`done` flag (plans stored before the table). Writing one small row per op keeps a run over a
large plan from rewriting the whole `ops` blob each time."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mixsync.db.models.library import Plan, PlanProgress


def done_flags(s: Session, plan: Plan) -> list[bool]:
    q = select(PlanProgress.op_index, PlanProgress.done).where(PlanProgress.plan_id == plan.id)
    rows: dict[int, bool] = {i: d for i, d in s.execute(q).all()}
    return [rows.get(i, bool(op.get("done"))) for i, op in enumerate(plan.ops)]


def with_done(s: Session, plan: Plan) -> list[dict[str, Any]]:
    """The plan's ops with their current `done` flag, for display."""
    return [{**op, "done": d} for op, d in zip(plan.ops, done_flags(s, plan), strict=True)]


def mark(s: Session, plan_id: int, i: int, done: bool) -> None:
    s.merge(PlanProgress(plan_id=plan_id, op_index=i, done=done))
