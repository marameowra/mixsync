"""The Request lifecycle: every status change and every set of statuses, in one place.

Transitions only set fields on the Request; the caller's job transaction commits them
(ADR 0004). Nothing here commits or enqueues.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mixsync.db.models.work import Request

SEARCHING = ("searching", "no_results", "partial")  # a search is the next step
WAITING = ("no_results", "partial")  # a repeat search is queued
IN_PROGRESS = (*SEARCHING, "downloading", "verifying", "importing")  # a failed job fails these
BUSY = ("downloading", "verifying", "importing")  # files are moving: cannot be deleted
OPEN = (*SEARCHING, "downloading", "verifying", "review")  # may still bring tracks in
DONE = ("imported", "owned", "failed", "skipped")  # will not bring a file
NO_FILE = ("failed", "skipped")  # DONE minus imported/owned: ended without a file


def can_search(req: "Request") -> bool:
    return req.status in SEARCHING and not req.from_files


def is_busy(req: "Request") -> bool:
    return req.status in BUSY


def is_waiting(req: "Request") -> bool:
    return req.status in WAITING


def is_dead_end(req: "Request") -> bool:
    """A discovery request whose release cannot give the wanted recording: asking again finds
    the same, so the row itself stops a rebuild re-requesting it."""
    return req.dead_end


def dead_recordings(req: "Request", ever: set[str]) -> list[str]:
    """The wanted recordings a dead end rules out. An import that brought some in rules out only
    those not in `ever` (the recordings that came in or are owned)."""
    return [m for m in req.recording_mbids if req.status == "failed" or m not in ever]


def failed(req: "Request", why: str) -> None:
    req.status, req.last_error = "failed", why


def owned(req: "Request") -> None:
    req.status, req.last_error = "owned", None


def dead_end(req: "Request", why: str) -> None:
    req.status, req.last_error, req.dead_end = "failed", why, True


def no_results(req: "Request") -> None:
    """A search found nothing. A partial album keeps its label while it waits for the rest."""
    if req.status != "partial":
        req.status = "no_results"


def downloading(req: "Request") -> None:
    req.status, req.no_results_attempts = "downloading", 0


def review(req: "Request", why: str | None = None) -> None:
    req.status = "review"
    if why:
        req.last_error = why


def rejected(req: "Request") -> None:
    req.status, req.last_error = "skipped", "rejected in review"


def download_failed(req: "Request", has_tracks: bool, why: str) -> None:
    """The candidate is out. Part of the album already in keeps it a partial request while the
    rest is sought; files identified from disk have nothing to search for."""
    if req.from_files:
        failed(req, why)
    elif has_tracks:
        head = f"partial: {req.partial_note}" if req.partial_note else "partial"
        req.status, req.last_error = "partial", f"{head}; {why}"
    else:
        req.status, req.last_error = "searching", why


def keep_looking(req: "Request", partial: str, notes: Sequence[str] = ()) -> None:
    """Some tracks are in ("N of M tracks present"); open until the rest of the album is here."""
    req.status, req.partial_note = "partial", partial
    req.last_error = "partial: " + "; ".join([partial, *notes])


def finished(
    req: "Request",
    errors: Sequence[str],
    notes: Sequence[str],
    *,
    unverified: bool = False,
    dead_end: bool = False,
) -> None:
    """An import ended. `dead_end`: the release lacks some wanted recordings (discovery)."""
    req.status = "failed" if errors else "unverified" if unverified else "imported"
    req.dead_end = dead_end and not errors
    req.partial_note = None
    req.last_error = "; ".join(notes) or None
