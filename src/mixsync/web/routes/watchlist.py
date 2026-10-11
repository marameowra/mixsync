import re
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select

from mixsync.core.capabilities import Capability
from mixsync.db.auth import AuthUser
from mixsync.db.models.watchlist import Follow, FollowHit, WatchedEntity
from mixsync.watchlist import follows
from mixsync.watchlist.poller import request_hit
from mixsync.web.deps import Db, Watch, require
from mixsync.web.templating import templates

router = APIRouter(prefix="/watchlist")
User = Annotated[AuthUser, Depends(require(Capability.request))]

_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
_DATE = re.compile(r"\d{4}(?:-\d{2}(?:-\d{2})?)?")


def _kind(kind: str) -> str:
    if kind not in follows.KINDS:
        raise HTTPException(400, "follow an artist or a label")
    return kind


def _own(db: Db, user: AuthUser, follow_id: int) -> Follow:
    f = db.get(Follow, follow_id)
    if f is None:
        raise HTTPException(404, "no such follow")
    if f.user_id != user.id:
        raise HTTPException(403, "not your follow")
    return f


@router.get("")
def follow_list(request: Request, user: User, db: Db) -> HTMLResponse:
    rows = db.execute(
        select(Follow, WatchedEntity)
        .join(WatchedEntity, WatchedEntity.id == Follow.entity_id)
        .where(Follow.user_id == user.id)
        .order_by(Follow.name)
    ).all()
    found = {
        fid: n
        for fid, n in db.execute(
            select(FollowHit.follow_id, func.count(FollowHit.id)).group_by(FollowHit.follow_id)
        )
    }
    ctx = {"user": user, "rows": rows, "found": found}
    return templates.TemplateResponse(request, "pages/watchlist.html", ctx)


@router.get("/search")
async def search(request: Request, user: User, watch: Watch, kind: str, q: str) -> HTMLResponse:
    results = await watch.search_entities(_kind(kind), q[:200])
    ctx = {"user": user, "kind": kind, "q": q, "results": results}
    return templates.TemplateResponse(request, "pages/watchlist_search.html", ctx)


@router.post("/follow")
async def follow(
    user: User,
    db: Db,
    watch: Watch,
    kind: Annotated[str, Form()],
    mbid: Annotated[str, Form()],
    name: Annotated[str, Form()] = "",
) -> RedirectResponse:
    m = _UUID.search(mbid)  # an MBID or a MusicBrainz page URL
    if not m:
        raise HTTPException(400, "not a MusicBrainz id or URL")
    mbid = str(uuid.UUID(m.group()))
    name = name.strip() or await watch.entity_name(_kind(kind), mbid)
    f = follows.add_follow(db, datetime.now(UTC), user.id, _kind(kind), mbid, name)
    return RedirectResponse(f"/watchlist/{f.id}", status_code=303)


@router.get("/{follow_id}")
def follow_page(request: Request, follow_id: int, user: User, db: Db) -> HTMLResponse:
    f = _own(db, user, follow_id)
    e = db.get(WatchedEntity, f.entity_id)
    hits = db.scalars(
        select(FollowHit).where(FollowHit.follow_id == f.id).order_by(FollowHit.id.desc())
    ).all()
    ctx = {"user": user, "f": f, "e": e, "hits": hits, "types": follows.RELEASE_TYPES}
    return templates.TemplateResponse(request, "pages/watchlist_follow.html", ctx)


@router.post("/{follow_id}/filters")
def set_filters(
    follow_id: int,
    user: User,
    db: Db,
    types: Annotated[list[str] | None, Form()] = None,
    only_after_follow: Annotated[str, Form()] = "",
    min_date: Annotated[str, Form()] = "",
    weekly_cap: Annotated[str, Form()] = "",
) -> RedirectResponse:
    f = _own(db, user, follow_id)
    min_date, weekly_cap = min_date.strip(), weekly_cap.strip()
    if min_date and not _DATE.fullmatch(min_date):
        raise HTTPException(400, "minimum date is YYYY, YYYY-MM or YYYY-MM-DD")
    if weekly_cap and not (weekly_cap.isascii() and weekly_cap.isdigit()):
        raise HTTPException(400, "weekly cap is a whole number, or empty for unlimited")
    f.release_types = [t for t in follows.RELEASE_TYPES if t in (types or [])]
    f.only_after_follow = bool(only_after_follow)
    f.min_date = min_date or None
    f.weekly_cap = int(weekly_cap) if weekly_cap else None
    return RedirectResponse(f"/watchlist/{f.id}", status_code=303)


@router.post("/{follow_id}/delete")
def delete_follow(follow_id: int, user: User, db: Db) -> RedirectResponse:
    follows.remove_follow(db, _own(db, user, follow_id))
    return RedirectResponse("/watchlist", status_code=303)


@router.post("/{follow_id}/check")
def check_now(follow_id: int, user: User, db: Db) -> RedirectResponse:
    f = _own(db, user, follow_id)
    e = db.get(WatchedEntity, f.entity_id)
    if e is None or not follows.check_now(db, datetime.now(UTC), e):
        raise HTTPException(409, "checked within the last hour")
    return RedirectResponse(f"/watchlist/{f.id}", status_code=303)


@router.post("/{follow_id}/hits/{hit_id}/request")
def request_skipped(follow_id: int, hit_id: int, user: User, db: Db) -> RedirectResponse:
    f = _own(db, user, follow_id)
    hit = db.get(FollowHit, hit_id)
    if hit is None or hit.follow_id != f.id:
        raise HTTPException(404, "no such release")
    if hit.status != "skipped_cap":
        raise HTTPException(409, f"release is {hit.status}")
    request_hit(db, datetime.now(UTC), f, hit)
    return RedirectResponse(f"/watchlist/{f.id}", status_code=303)
