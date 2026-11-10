"""The approval queue: requests from users without automatic downloads wait here (#296)."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from mixsync.acquire.requests import NotAwaiting, approve, deny
from mixsync.core.capabilities import Capability
from mixsync.db.auth import AuthUser
from mixsync.db.models.auth import User
from mixsync.db.models.work import Request as AlbumRequest
from mixsync.web.deps import Db, Namer, require
from mixsync.web.names import resolve
from mixsync.web.templating import templates

router = APIRouter(prefix="/approvals")
Approver = Annotated[AuthUser, Depends(require(Capability.approve))]
MAX_REASON = 500


@router.get("")
async def queue(request: Request, user: Approver, db: Db, namer: Namer) -> HTMLResponse:
    rows = db.execute(
        select(AlbumRequest, User.username)
        .join(User, User.id == AlbumRequest.user_id)
        .where(AlbumRequest.status == "pending_approval")
        .order_by(AlbumRequest.created_at, AlbumRequest.id)
    ).all()
    names = await resolve(db, namer, {("release", r.release_mbid) for r, _ in rows})
    return templates.TemplateResponse(
        request, "pages/approvals.html", {"user": user, "rows": rows, "names": names}
    )


def _awaiting(db: Session, request_id: int) -> AlbumRequest:
    req = db.get(AlbumRequest, request_id, with_for_update=True)  # a double click approves once
    if req is None:
        raise HTTPException(404, "no such request")
    return req


@router.post("/{request_id}/approve")
def approve_request(request_id: int, user: Approver, db: Db) -> RedirectResponse:
    try:
        approve(db, datetime.now(UTC), _awaiting(db, request_id))
    except NotAwaiting as e:
        raise HTTPException(409, str(e)) from None
    return RedirectResponse("/approvals", 303)


@router.post("/{request_id}/deny")
def deny_request(
    request_id: int, user: Approver, db: Db, reason: Annotated[str, Form()] = ""
) -> RedirectResponse:
    try:
        deny(_awaiting(db, request_id), reason.strip()[:MAX_REASON])
    except NotAwaiting as e:
        raise HTTPException(409, str(e)) from None
    return RedirectResponse("/approvals", 303)
