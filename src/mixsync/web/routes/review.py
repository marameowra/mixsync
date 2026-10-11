import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from mixsync.acquire.requests import apply_review
from mixsync.core.capabilities import Capability
from mixsync.db import decisions
from mixsync.db.auth import AuthUser
from mixsync.db.models.matching import MatchDecision
from mixsync.library import tags
from mixsync.match.candidates import parse_name
from mixsync.web.deps import Db, decision_for, require
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


@router.get("")
def review_list(request: Request, user: User, db: Db) -> HTMLResponse:
    owner = None if Capability.approve in user.capabilities else user.id
    items = decisions.pending_for(db, owner)
    return templates.TemplateResponse(
        request, "pages/review_list.html", {"user": user, "items": items}
    )


@router.get("/{decision_id}")
def review_detail(request: Request, decision_id: int, user: User, db: Db) -> HTMLResponse:
    d = decision_for(db, user, decision_id)
    ctx = {"user": user, "d": d, "prefill": _prefill(d)}
    return templates.TemplateResponse(request, "pages/review_detail.html", ctx)


def _prefill(d: MatchDecision) -> dict[str, Any]:
    """Editable tags for Mark unverified: each downloaded file's own tags, else its file name."""
    files = d.evidence.get("source", {}).get("files", [])
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


def _act(
    db: Db, user: AuthUser, decision_id: int, action: str, mbid: str | None = None
) -> RedirectResponse:
    decision_for(db, user, decision_id)
    try:
        decisions.act(db, decision_id, user.id, action, mbid)
    except decisions.DecisionStateError as e:
        raise HTTPException(409, str(e)) from e
    apply_review(db, datetime.now(UTC), decision_id)
    return RedirectResponse("/review", status_code=303)


@router.post("/{decision_id}/accept")
def accept(decision_id: int, user: User, db: Db) -> RedirectResponse:
    return _act(db, user, decision_id, "accept")


@router.post("/{decision_id}/reject")
def reject(decision_id: int, user: User, db: Db) -> RedirectResponse:
    return _act(db, user, decision_id, "reject")


@router.post("/{decision_id}/repick")
def repick(
    decision_id: int, user: User, db: Db, release: Annotated[str, Form()]
) -> RedirectResponse:
    return _act(db, user, decision_id, "repick", parse_release_mbid(release))


def _int(text: str, what: str, minimum: int = 1) -> int:
    if not text.strip().isdecimal() or not minimum <= int(text) <= 9999:
        raise HTTPException(400, f"{what} must be a whole number from {minimum} to 9999")
    return int(text)


def _text(text: str, what: str, required: bool = True) -> str:
    text = text.strip()
    if (required and not text) or len(text) > _TEXT_MAX:
        raise HTTPException(400, f"{what} is required and at most {_TEXT_MAX} characters")
    return text


@router.post("/{decision_id}/unverified")
def unverified(
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
) -> RedirectResponse:
    d = decision_for(db, user, decision_id)
    files = d.evidence.get("source", {}).get("files", [])
    if not files or any(not f.get("local_path") for f in files):
        raise HTTPException(409, "the downloaded files are not known")
    if not len(files) == len(title) == len(artist) == len(disc) == len(track):
        raise HTTPException(400, "one set of fields per file is needed")
    edits = {
        "albumartist": _text(albumartist, "album artist"),
        "album": _text(album, "album"),
        "year": _int(year, "year") if year.strip() else None,
        "tracks": [
            {
                "local_path": f["local_path"],
                "title": _text(ti, "title"),
                "artist": _text(ar, "artist", required=False),
                "disc": _int(di, "disc"),
                "track": _int(tr, "track"),
            }
            for f, ti, ar, di, tr in zip(files, title, artist, disc, track, strict=True)
        ],
    }
    try:
        decisions.act(db, decision_id, user.id, "unverified")
    except decisions.DecisionStateError as e:
        raise HTTPException(409, str(e)) from e
    d.evidence = {**d.evidence, "unverified": edits}  # same transaction as the action
    apply_review(db, datetime.now(UTC), decision_id)
    return RedirectResponse("/review", status_code=303)
