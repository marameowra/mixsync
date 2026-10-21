import re
import unicodedata
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from mixsync.acquire import review
from mixsync.acquire.evidence import Evidence
from mixsync.core.capabilities import Capability
from mixsync.db import decisions
from mixsync.db.auth import AuthUser
from mixsync.db.models.matching import MatchDecision
from mixsync.library import tags
from mixsync.match.candidates import parse_name
from mixsync.web.deps import Db, Namer, decision_for, require
from mixsync.web.names import MAX_LOOKUPS, resolve
from mixsync.web.templating import templates

router = APIRouter(prefix="/review")
User = Annotated[AuthUser, Depends(require(Capability.request))]

_TEXT_MAX = 255
_MBID = re.compile(
    r"(?:https?://(?:www\.)?musicbrainz\.org/release/)?([0-9a-fA-F-]{36})(?:[/?#].*)?", re.S
)


def parse_release_mbid(text: str) -> str:
    m = _MBID.fullmatch(text.strip())
    try:
        if m:
            return str(uuid.UUID(m.group(1)))
    except ValueError:
        pass
    raise HTTPException(400, "not a MusicBrainz release URL or MBID")


def _owner(user: AuthUser) -> int | None:
    return None if Capability.approve in user.capabilities else user.id


def _target(d: MatchDecision, key: str) -> str:
    return str(Evidence.parse(d.evidence).target.get(key, "")).casefold()


_SORTS: dict[str, Callable[[MatchDecision], Any]] = {
    "date": lambda d: (d.created_at, d.id),
    "score": lambda d: d.distance,
    "artist": lambda d: _target(d, "artist"),
    "album": lambda d: _target(d, "title"),
    "match": lambda d: bool(d.release_mbid),
}


def _count(text: str) -> int:
    return int(text) if text.isascii() and text.isdigit() and len(text) <= 6 else 0


@router.get("")
def review_list(
    request: Request,
    user: User,
    db: Db,
    sort: str = "date",
    order: str = "asc",
    requeued: str | None = None,
    missing: str = "",
    busy: str = "",
    stale: str = "",
) -> HTMLResponse:
    items = decisions.pending_for(db, _owner(user))
    key = _SORTS.get(sort, _SORTS["date"])
    items.sort(key=key, reverse=order == "desc")
    ctx: dict[str, Any] = {"user": user, "items": items, "sort": sort, "order": order}
    if requeued is not None:  # the summary of a re-check, carried in the redirect
        n = {
            k: _count(v)
            for k, v in (
                ("requeued", requeued),
                ("missing", missing),
                ("busy", busy),
                ("stale", stale),
            )
        }
        ctx["n"] = n
    return templates.TemplateResponse(request, "pages/review_list.html", ctx)


@router.post("/recheck")
def recheck(user: User, db: Db) -> RedirectResponse:
    """Verify every pending review again with the current scorer: the decision is closed and the
    files go back through VERIFY against the release the request was given (a repick, or none
    for a migration that identified it). No network here."""
    now = datetime.now(UTC)
    n = Counter(
        review.recheck(db, now, d.id, user.id) for d in decisions.pending_for(db, _owner(user))
    )
    queued, missing, busy, stale = n["queued"], n["missing"], n["busy"], n["stale"]
    return RedirectResponse(
        f"/review?requeued={queued}&missing={missing}&busy={busy}&stale={stale}", status_code=303
    )


@router.get("/{decision_id}")
async def review_detail(
    request: Request, decision_id: int, user: User, db: Db, namer: Namer
) -> HTMLResponse:
    d = decision_for(db, user, decision_id)
    refs = {("release", d.release_mbid)} | {
        ("recording", r) for r in Evidence.parse(d.evidence).acoustid_recording_ids
    }
    names = await resolve(db, namer, refs, MAX_LOOKUPS)
    ctx = {"user": user, "d": d, "prefill": _prefill(d), "names": names}
    return templates.TemplateResponse(request, "pages/review_detail.html", ctx)


def _prefill(d: MatchDecision) -> dict[str, Any]:
    """Editable tags for Mark unverified: each downloaded file's own tags, else its file name."""
    files = Evidence.parse(d.evidence).files
    found: list[dict[str, str]] = []
    for f in files:
        try:
            found.append(tags.read_fields(Path(f["local_path"])))
        except Exception:  # missing or unreadable: the file name is all we have
            found.append({})
    rows: list[dict[str, str]] = []
    for i, (f, t) in enumerate(zip(files, found, strict=True), 1):
        disc, track, title = parse_name(Path(f.get("name") or "").stem)
        rows.append(
            {
                "name": f.get("name", ""),
                "title": t.get("title") or title,
                "artist": t.get("artist", ""),
                "disc": t.get("discnumber") or str(disc or 1),
                "track": t.get("tracknumber") or str(track or i),
            }
        )

    def first(key: str) -> str:
        return next((t[key] for t in found if t.get(key)), "")

    return {
        "albumartist": first("albumartist") or first("artist"),
        "album": first("album"),
        "year": first("date")[:4],
        "rows": rows,
    }


def _done(op: Callable[[], None], db: Db, user: AuthUser) -> RedirectResponse:
    try:
        op()
    except review.Refused as e:
        raise HTTPException(409, str(e)) from e
    return _next(db, user)


def _next(db: Db, user: AuthUser) -> RedirectResponse:
    """Open the next pending review, else the list."""
    pending = decisions.pending_for(db, _owner(user))
    return RedirectResponse(f"/review/{pending[0].id}" if pending else "/review", status_code=303)


@router.post("/{decision_id}/accept")
def accept(decision_id: int, user: User, db: Db) -> RedirectResponse:
    decision_for(db, user, decision_id)
    return _done(lambda: review.accept(db, datetime.now(UTC), decision_id, user.id), db, user)


@router.post("/{decision_id}/reject")
def reject(decision_id: int, user: User, db: Db) -> RedirectResponse:
    decision_for(db, user, decision_id)
    return _done(lambda: review.reject(db, datetime.now(UTC), decision_id, user.id), db, user)


@router.post("/{decision_id}/repick")
def repick(
    decision_id: int, user: User, db: Db, release: Annotated[str, Form()]
) -> RedirectResponse:
    decision_for(db, user, decision_id)
    mbid = parse_release_mbid(release)
    return _done(lambda: review.repick(db, datetime.now(UTC), decision_id, user.id, mbid), db, user)


def _int(text: str, what: str, minimum: int = 1) -> int:
    if not text.strip().isdecimal() or not minimum <= int(text) <= 9999:
        raise ValueError(f"{what} must be a whole number from {minimum} to 9999")
    return int(text)


def _text(text: str, what: str, required: bool = True) -> str:
    text = text.strip()
    if required and not text:
        raise ValueError(f"{what} is required")
    if len(text) > _TEXT_MAX:
        raise ValueError(f"{what} is at most {_TEXT_MAX} characters")
    if any(unicodedata.category(c) == "Cc" for c in text):
        raise ValueError(f"{what} must not contain control characters")
    return text


@router.post("/{decision_id}/unverified", response_model=None)
def unverified(
    request: Request,
    decision_id: int,
    user: User,
    db: Db,
    albumartist: Annotated[str, Form()],
    album: Annotated[str, Form()],
    title: Annotated[list[str], Form()],
    artist: Annotated[list[str], Form()],
    disc: Annotated[list[str], Form()],
    track: Annotated[list[str], Form()],
    year: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    d = decision_for(db, user, decision_id)
    files = Evidence.parse(d.evidence).files
    errors: list[str] = []

    def ok(fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
        try:
            return fn(*args, **kw)
        except ValueError as e:
            errors.append(str(e))

    if not len(files) == len(title) == len(artist) == len(disc) == len(track):
        errors.append("one set of fields per file is needed")
    edits = {
        "albumartist": ok(_text, albumartist, "album artist"),
        "album": ok(_text, album, "album"),
        "year": ok(_int, year, "year") if year.strip() else None,
        "tracks": [
            {
                "local_path": f.get("local_path", ""),
                "title": ok(_text, ti, f"{f.get('name', '')}: title"),
                "artist": ok(_text, ar, f"{f.get('name', '')}: artist", required=False),
                "disc": ok(_int, di, f"{f.get('name', '')}: disc"),
                "track": ok(_int, tr, f"{f.get('name', '')}: track"),
            }
            for f, ti, ar, di, tr in zip(files, title, artist, disc, track, strict=False)
        ],
    }
    if errors:  # show the form again with what was typed
        sent = {
            "albumartist": albumartist,
            "album": album,
            "year": year,
            "rows": [
                {"name": f.get("name", ""), "title": ti, "artist": ar, "disc": di, "track": tr}
                for f, ti, ar, di, tr in zip(files, title, artist, disc, track, strict=False)
            ],
        }
        ctx = {"user": user, "d": d, "prefill": sent, "errors": errors}
        return templates.TemplateResponse(request, "pages/review_detail.html", ctx, status_code=400)
    return _done(
        lambda: review.unverified(db, datetime.now(UTC), decision_id, user.id, edits), db, user
    )
