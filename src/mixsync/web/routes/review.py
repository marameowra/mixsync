import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from mixsync.core.capabilities import Capability
from mixsync.db import decisions
from mixsync.db.auth import AuthUser
from mixsync.web.deps import Db, decision_for, require
from mixsync.web.templating import templates

router = APIRouter(prefix="/review")
User = Annotated[AuthUser, Depends(require(Capability.request))]

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
    return templates.TemplateResponse(request, "pages/review_detail.html", {"user": user, "d": d})


def _act(
    db: Db, user: AuthUser, decision_id: int, action: str, mbid: str | None = None
) -> RedirectResponse:
    decision_for(db, user, decision_id)
    try:
        decisions.act(db, decision_id, user.id, action, mbid)
    except decisions.DecisionStateError as e:
        raise HTTPException(409, str(e)) from e
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
