from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from mixsync.core.capabilities import Capability
from mixsync.db.auth import AuthUser
from mixsync.db.models.discovery import DiscoveryPlaylist
from mixsync.discovery import playlists
from mixsync.web.deps import Db, require
from mixsync.web.templating import templates

router = APIRouter(prefix="/discover")
User = Annotated[AuthUser, Depends(require(Capability.request))]


@router.get("")
def playlist_list(request: Request, user: User, db: Db) -> HTMLResponse:
    playlists.ensure_default(db, datetime.now(UTC), user.id)  # a first look after linking
    rows = db.scalars(
        select(DiscoveryPlaylist)
        .where(DiscoveryPlaylist.user_id == user.id)
        .order_by(DiscoveryPlaylist.id)
    ).all()
    return templates.TemplateResponse(request, "pages/discover.html", {"user": user, "rows": rows})


@router.post("/{playlist_id}/build")
def build_now(playlist_id: int, user: User, db: Db) -> RedirectResponse:
    pl = db.get(DiscoveryPlaylist, playlist_id)
    if pl is None:
        raise HTTPException(404, "no such playlist")
    if pl.user_id != user.id:
        raise HTTPException(403, "not your playlist")
    if not playlists.build_now(db, datetime.now(UTC), pl):
        raise HTTPException(409, "built within the last hour")
    return RedirectResponse("/discover", status_code=303)
