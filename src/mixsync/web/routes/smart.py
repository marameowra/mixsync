"""Smart playlists: create, edit rules and cadence, rebuild now, delete. Owned library only;
nothing here or in the build makes a request."""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from mixsync.core.capabilities import Capability
from mixsync.db.auth import AuthUser
from mixsync.db.models.discovery import SmartPlaylist
from mixsync.discovery import smart
from mixsync.web.deps import Db, require
from mixsync.web.templating import templates

router = APIRouter(prefix="/smart")
User = Annotated[AuthUser, Depends(require(Capability.request))]
INTS = ("year_from", "year_to", "added_days", "unplayed_days")


def _own(db: Db, user: AuthUser, playlist_id: int) -> SmartPlaylist:
    pl = db.get(SmartPlaylist, playlist_id)
    if pl is None:
        raise HTTPException(404, "no such playlist")
    if pl.user_id != user.id:
        raise HTTPException(403, "not your playlist")
    return pl


def _int(form: dict[str, str], key: str, lo: int, hi: int) -> int | None:
    v = form.get(key, "").strip()
    if not v:
        return None
    if not (v.isascii() and v.isdigit() and lo <= int(v) <= hi):
        raise HTTPException(400, f"{key.replace('_', ' ')} is a whole number from {lo} to {hi}")
    return int(v)


async def _fields(request: Request) -> dict[str, Any]:
    """The form, checked: every number a whole number in range, the year range in order."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    name = form.get("name", "").strip()
    if not 1 <= len(name) <= 200:
        raise HTTPException(400, "name is 1 to 200 characters")
    rules: dict[str, Any] = {}
    if genre := form.get("genre", "").strip()[:100]:
        rules["genre"] = genre
    for key in INTS:
        if (n := _int(form, key, 0, 9999 if key.startswith("year") else 36500)) is not None:
            rules[key] = n
    if rules.get("year_from", 0) > rules.get("year_to", 9999):
        raise HTTPException(400, "the year range must run from the earlier year to the later")
    if form.get("starred"):
        rules["starred"] = True
    sort = form.get("sort", "random")
    if sort not in smart.SORTS:
        raise HTTPException(400, "unknown sort")
    size = _int(form, "size", 1, 1000)
    cadence = _int(form, "cadence_days", 1, 365)
    if size is None or cadence is None:
        raise HTTPException(400, "size and cadence are required")
    return {"name": name, "rules": rules, "sort": sort, "size": size, "cadence_days": cadence}


@router.get("")
def playlist_list(request: Request, user: User, db: Db) -> HTMLResponse:
    rows = db.scalars(
        select(SmartPlaylist).where(SmartPlaylist.user_id == user.id).order_by(SmartPlaylist.id)
    ).all()
    ctx = {"user": user, "rows": rows, "sorts": smart.SORTS}
    return templates.TemplateResponse(request, "pages/smart.html", ctx)


def _save(db: Db, pl: SmartPlaylist, fields: dict[str, Any]) -> None:
    for k, v in fields.items():
        setattr(pl, k, v)
    try:
        with db.begin_nested():
            db.add(pl)
    except IntegrityError:
        raise HTTPException(409, f"you already have a smart playlist named {pl.name!r}") from None


@router.post("")
async def create(request: Request, user: User, db: Db) -> RedirectResponse:
    pl = SmartPlaylist(user_id=user.id, entries=[])
    _save(db, pl, await _fields(request))
    smart.ensure_build(db, datetime.now(UTC), pl)
    return RedirectResponse("/smart", status_code=303)


@router.post("/{playlist_id}/settings")
async def settings(playlist_id: int, request: Request, user: User, db: Db) -> RedirectResponse:
    """New rules apply from the next build; Rebuild now applies them at once."""
    pl = _own(db, user, playlist_id)
    _save(db, pl, await _fields(request))
    return RedirectResponse("/smart", status_code=303)


@router.post("/{playlist_id}/build")
def rebuild_now(playlist_id: int, user: User, db: Db) -> RedirectResponse:
    pl = _own(db, user, playlist_id)
    if not smart.build_now(db, datetime.now(UTC), pl):
        raise HTTPException(409, "built within the last hour")
    return RedirectResponse("/smart", status_code=303)


@router.post("/{playlist_id}/delete")
def delete(playlist_id: int, user: User, db: Db) -> RedirectResponse:
    db.delete(_own(db, user, playlist_id))  # its queued build finds no row and stops
    return RedirectResponse("/smart", status_code=303)
