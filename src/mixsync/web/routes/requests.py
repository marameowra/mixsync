from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from mixsync.acquire.requests import create_request
from mixsync.core.capabilities import Capability
from mixsync.db.auth import AuthUser
from mixsync.db.models.work import Request as AlbumRequest
from mixsync.web.deps import Db, require
from mixsync.web.routes.review import parse_release_mbid
from mixsync.web.templating import templates

router = APIRouter(prefix="/requests")
User = Annotated[AuthUser, Depends(require(Capability.request))]


@router.get("")
def request_list(request: Request, user: User, db: Db) -> HTMLResponse:
    items = db.scalars(
        select(AlbumRequest).where(AlbumRequest.user_id == user.id).order_by(AlbumRequest.id.desc())
    ).all()
    return templates.TemplateResponse(
        request, "pages/requests.html", {"user": user, "items": items}
    )


@router.post("")
def new_request(user: User, db: Db, release: Annotated[str, Form()]) -> RedirectResponse:
    create_request(db, datetime.now(UTC), user.id, parse_release_mbid(release))
    return RedirectResponse("/requests", status_code=303)
