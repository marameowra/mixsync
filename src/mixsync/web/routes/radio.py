"""Radio: start one from a track, album, artist or playlist page; edit size and cadence, rebuild
now, delete. Owned, analyzed tracks only; nothing here or in the build makes a request."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from mixsync.core.capabilities import Capability
from mixsync.db.auth import AuthUser
from mixsync.db.models.discovery import RadioPlaylist
from mixsync.discovery import radio, smart
from mixsync.web.deps import Db, require
from mixsync.web.templating import templates

router = APIRouter(prefix="/radio")
User = Annotated[AuthUser, Depends(require(Capability.request))]


def _own(db: Db, user: AuthUser, playlist_id: int) -> RadioPlaylist:
    pl = db.get(RadioPlaylist, playlist_id)
    if pl is None:
        raise HTTPException(404, "no such Radio")
    if pl.user_id != user.id:
        raise HTTPException(403, "not your Radio")
    return pl


def _whole(value: str, name: str, lo: int, hi: int) -> int:
    v = value.strip()
    if not (v.isascii() and v.isdigit() and lo <= int(v) <= hi):
        raise HTTPException(400, f"{name} is a whole number from {lo} to {hi}")
    return int(v)


def _save(db: Db, pl: RadioPlaylist, name: str) -> None:
    pl.name = name.strip()[:200]  # a seed page fills it in from a tag or path
    if not pl.name:
        raise HTTPException(400, "name is 1 to 200 characters")
    try:
        with db.begin_nested():
            db.add(pl)
    except IntegrityError:
        raise HTTPException(409, f"you already have a Radio named {pl.name!r}") from None


@router.get("")
def playlist_list(request: Request, user: User, db: Db) -> HTMLResponse:
    rows = db.scalars(
        select(RadioPlaylist).where(RadioPlaylist.user_id == user.id).order_by(RadioPlaylist.id)
    ).all()
    ctx = {
        "user": user,
        "rows": rows,
        "per_artist": radio.MAX_PER_ARTIST,
        "per_album": radio.MAX_PER_ALBUM,
    }
    return templates.TemplateResponse(request, "pages/radio.html", ctx)


@router.post("")
def create(
    user: User,
    db: Db,
    seed_kind: Annotated[str, Form()],
    seed_ref: Annotated[str, Form()],
    name: Annotated[str, Form()],
) -> RedirectResponse:
    """Refused unless the seed has an owned, analyzed track. Defaults are Discovery's."""
    try:
        radio.check_seed(db, user.id, seed_kind, seed_ref)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    pl = RadioPlaylist(user_id=user.id, seed_kind=seed_kind, seed_ref=seed_ref, entries=[])
    _save(db, pl, name)
    smart.ensure_build(db, datetime.now(UTC), pl)
    return RedirectResponse("/radio", status_code=303)


@router.post("/{playlist_id}/settings")
def settings(
    playlist_id: int,
    user: User,
    db: Db,
    name: Annotated[str, Form()],
    size: Annotated[str, Form()],
    cadence_days: Annotated[str, Form()],
) -> RedirectResponse:
    """New settings apply from the next build; Rebuild now applies them at once."""
    pl = _own(db, user, playlist_id)
    pl.size = _whole(size, "size", 1, 1000)
    pl.cadence_days = _whole(cadence_days, "cadence", 1, 365)
    _save(db, pl, name)
    return RedirectResponse("/radio", status_code=303)


@router.post("/{playlist_id}/build")
def rebuild_now(playlist_id: int, user: User, db: Db) -> RedirectResponse:
    pl = _own(db, user, playlist_id)
    if not smart.build_now(db, datetime.now(UTC), pl):
        raise HTTPException(409, "built within the last hour")
    return RedirectResponse("/radio", status_code=303)


@router.post("/{playlist_id}/delete")
def delete(playlist_id: int, user: User, db: Db) -> RedirectResponse:
    db.delete(_own(db, user, playlist_id))  # its queued build finds no row and stops
    return RedirectResponse("/radio", status_code=303)
