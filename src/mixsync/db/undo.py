"""When a merge or promote plan can still be undone. The purge, the UI and undo_plan all ask
here, so they cannot disagree."""

from datetime import datetime, timedelta

from mixsync.core.clock import aware
from mixsync.db.models.library import Plan

# A failed or unfinished plan may hold the only copy of a file in the trash: never windowed.
OPEN = ("running", "undoing", "failed")
UNDOABLE_STATUSES = (*OPEN, "done")


def undoable(plan: Plan, now: datetime, days: int) -> bool:
    """Open plans always; a done plan for `days` days after its last change."""
    if plan.status in OPEN:
        return True
    if plan.status != "done":
        return False
    return now - aware(plan.updated_at) < timedelta(days=days)
