"""When a merge or promote plan can still be undone. The purge, the UI and undo_plan all ask
here, so they cannot disagree."""

from datetime import datetime, timedelta

from mixsync.db.models.library import Plan

IN_PROGRESS = ("running", "undoing")
FINISHED = ("done", "failed")  # undoable for `days` after the plan's last change
UNDOABLE_STATUSES = (*IN_PROGRESS, *FINISHED)


def undoable(plan: Plan, now: datetime, days: int) -> bool:
    if plan.status in IN_PROGRESS:
        return True
    return plan.status in FINISHED and now - plan.updated_at.replace(tzinfo=now.tzinfo) < timedelta(
        days=days
    )
