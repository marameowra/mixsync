"""The Request lifecycle: every status change and every set of statuses, in one place.

Transitions only set fields on the Request; the caller's job transaction commits them
(ADR 0004). Nothing here commits or enqueues.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from mixsync.db.models.work import Status

if TYPE_CHECKING:
    from mixsync.db.models.work import Request

__all__ = ["Status"]  # defined beside the column it types (db cannot import acquire)

SEARCHING: tuple[Status, ...] = ("searching", "no_results", "partial")  # a search is next
WAITING: tuple[Status, ...] = ("no_results", "partial")  # a repeat search is queued
IN_PROGRESS: tuple[Status, ...] = (
    *SEARCHING,
    "downloading",
    "verifying",
    "importing",
)  # a failed job fails these
BUSY: tuple[Status, ...] = ("downloading", "verifying", "importing")  # files moving: no delete
OPEN: tuple[Status, ...] = (*SEARCHING, "downloading", "verifying", "review")  # may bring tracks
DONE: tuple[Status, ...] = (
    "imported",
    "owned",
    "failed",
    "skipped",
    "unverified",
)  # no file to come
NO_FILE: tuple[Status, ...] = ("failed", "skipped")  # ended without a file (not in DONE's rest)


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


def _set(req: "Request", status: Status, *, keep_note: bool = False) -> None:
    """Every transition goes through here: dead_end and partial_note are cleared unless a
    transition sets or keeps them."""
    req.status, req.dead_end = status, False
    if not keep_note:
        req.partial_note = None


def failed(req: "Request", why: str) -> None:
    _set(req, "failed")
    req.last_error = why


def owned(req: "Request") -> None:
    _set(req, "owned")
    req.last_error = None


def dead_end(req: "Request", why: str) -> None:
    _set(req, "failed")
    req.last_error, req.dead_end = why, True


def no_results(req: "Request") -> None:
    """A search found nothing. A partial album keeps its label while it waits for the rest."""
    _set(req, req.status if req.status == "partial" else "no_results", keep_note=True)


def downloading(req: "Request") -> None:
    _set(req, "downloading", keep_note=True)  # a partial request fetches its next candidate
    req.no_results_attempts = 0


def verifying(req: "Request") -> None:
    _set(req, "verifying", keep_note=True)


def importing(req: "Request") -> None:
    _set(req, "importing", keep_note=True)


def review(req: "Request", why: str | None = None) -> None:
    _set(req, "review")
    if why:
        req.last_error = why


def rejected(req: "Request") -> None:
    _set(req, "skipped")
    req.last_error = "rejected in review"


def download_failed(req: "Request", has_tracks: bool, why: str) -> None:
    """The candidate is out. Part of the album already in keeps it a partial request while the
    rest is sought; files identified from disk have nothing to search for."""
    if req.from_files:
        failed(req, why)
    elif has_tracks:
        head = f"partial: {req.partial_note}" if req.partial_note else "partial"
        _set(req, "partial", keep_note=True)
        req.last_error = f"{head}; {why}"
    else:
        _set(req, "searching")
        req.last_error = why


def job_failed(req: "Request", has_tracks: bool, why: str) -> None:
    """A job for this request failed for good. Tracks already in keep it partial (the error is
    recorded, the tracks stay); otherwise it fails. Either way the failure is not a dead end."""
    if has_tracks:
        head = f"partial: {req.partial_note}" if req.partial_note else "partial"
        _set(req, "partial", keep_note=True)
        req.last_error = f"{head}; {why}"
    else:
        failed(req, why)


def keep_looking(req: "Request", partial: str, notes: Sequence[str] = ()) -> None:
    """Some tracks are in ("N of M tracks present"); open until the rest of the album is here."""
    _set(req, "partial")
    req.partial_note = partial
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
    _set(req, "failed" if errors else "unverified" if unverified else "imported")
    req.dead_end = dead_end and not errors
    req.last_error = "; ".join(notes) or None
