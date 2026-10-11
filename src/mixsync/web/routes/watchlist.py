import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mixsync.core.capabilities import Capability
from mixsync.core.types import ReleaseGroupHit
from mixsync.db.auth import AuthUser
from mixsync.db.models.watchlist import Follow, FollowHit, WatchedEntity
from mixsync.watchlist import follows
from mixsync.watchlist.poller import is_owned, is_requested, queue_picks, request_hit
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
async def search(
    request: Request, user: User, db: Db, watch: Watch, kind: str, q: str
) -> HTMLResponse:
    db.commit()  # the login's last_seen write must not hold the SQLite lock during the bucket's
    results = await watch.search_entities(_kind(kind), q[:200])
    ctx = {"user": user, "kind": kind, "q": q, "results": results}
    return templates.TemplateResponse(request, "pages/watchlist_search.html", ctx)


@router.post("/follow")
async def follow(
    request: Request,
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
    name = name.strip()
    db.commit()  # see search(); the session is spent, so the follow is written in a fresh one
    if not name:
        name = await watch.entity_name(_kind(kind), mbid)
    with request.app.state.session_factory.begin() as s:
        f = follows.add_follow(s, datetime.now(UTC), user.id, _kind(kind), mbid, name)
        fid = f.id
    picker = "/catalog" if kind == "artist" else ""  # offer an artist's back catalogue
    return RedirectResponse(f"/watchlist/{fid}{picker}", status_code=303)


@router.get("/{follow_id}")
def follow_page(
    request: Request, follow_id: int, user: User, db: Db, done: str = "", n: int = 0
) -> HTMLResponse:
    f = _own(db, user, follow_id)
    e = db.get(WatchedEntity, f.entity_id)
    hits = db.scalars(
        select(FollowHit).where(FollowHit.follow_id == f.id).order_by(FollowHit.id.desc())
    ).all()
    ctx = {
        "user": user,
        "f": f,
        "e": e,
        "hits": hits,
        "types": follows.RELEASE_TYPES,
        "msg": _DONE.get(done, "").format(n=n),
    }
    return templates.TemplateResponse(request, "pages/watchlist_follow.html", ctx)


@router.post("/{follow_id}/filters")
def set_filters(
    follow_id: int,
    user: User,
    db: Db,
    types: Annotated[list[str] | None, Form()] = None,
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
    if hit.status not in ("skipped_cap", "skipped_filter"):
        raise HTTPException(409, f"release is {hit.status}")
    if is_owned(db, hit.release_group_mbid) or is_requested(
        db, f.user_id, hit.release_group_mbid, (hit.release_mbid,)
    ):
        return RedirectResponse(f"/watchlist/{f.id}?done=refused", status_code=303)
    request_hit(db, datetime.now(UTC), f, hit)
    return RedirectResponse(f"/watchlist/{f.id}", status_code=303)


_DONE = {"queued": "Queued {n} for request.", "refused": "Already owned or requested."}
_TYPES = ("album", "ep", "single")  # then the rest, as "other"


def _disabled(db: Session, user_id: int, h: ReleaseGroupHit) -> str:
    """Why a release group cannot be picked, or ""."""
    if is_owned(db, h.group_id):
        return "owned"
    status = db.scalar(
        select(FollowHit.status).where(
            FollowHit.release_group_mbid == h.group_id,
            FollowHit.status.in_(("requested", "skipped_requested", "picked")),
            FollowHit.follow_id.in_(select(Follow.id).where(Follow.user_id == user_id)),
        )
    )
    return {None: "", "picked": "queued"}.get(status, "already requested")


def _artist_follow(db: Db, user: AuthUser, follow_id: int) -> tuple[Follow, WatchedEntity]:
    f = _own(db, user, follow_id)
    e = db.get(WatchedEntity, f.entity_id)
    if e is None or e.kind != "artist":
        raise HTTPException(404, "only an artist has a back catalogue")
    return f, e


@router.get("/{follow_id}/catalog")
async def catalog(
    request: Request, follow_id: int, user: User, db: Db, watch: Watch
) -> HTMLResponse:
    f, e = _artist_follow(db, user, follow_id)
    follow_ref, mbid = SimpleNamespace(id=f.id, name=f.name), e.mbid
    db.commit()  # see search(); the session is spent after this
    found, truncated = await watch.artist_discography(mbid)
    groups: dict[str, list[tuple[ReleaseGroupHit, str]]] = {}
    with request.app.state.session_factory() as s:
        for h in sorted(found, key=lambda h: h.date or "", reverse=True):  # undated last
            key = h.type if h.type in _TYPES else "other"
            groups.setdefault(key, []).append((h, _disabled(s, user.id, h)))
    order = [(k, groups[k]) for k in (*_TYPES, "other") if k in groups]
    ctx = {"user": user, "f": follow_ref, "groups": order, "truncated": truncated}
    return templates.TemplateResponse(request, "pages/watchlist_catalog.html", ctx)


@router.post("/{follow_id}/catalog")
async def catalog_submit(
    request: Request,
    follow_id: int,
    user: User,
    db: Db,
    watch: Watch,
    groups: Annotated[list[str] | None, Form()] = None,
) -> RedirectResponse:
    """Queue the checked release groups. The ids are checked against the artist's discography
    (cached from the page); the requests are made by the worker, not here."""
    f, e = _artist_follow(db, user, follow_id)
    fid, mbid = f.id, e.mbid
    db.commit()  # see search(); the session is spent after this
    found, _ = await watch.artist_discography(mbid)
    wanted = set(groups or [])
    with request.app.state.session_factory.begin() as s:
        picks = [h for h in found if h.group_id in wanted and not _disabled(s, user.id, h)]
        if (fresh := s.get(Follow, fid)) is not None:
            queue_picks(s, datetime.now(UTC), fresh, picks)
    return RedirectResponse(f"/watchlist/{fid}?done=queued&n={len(picks)}", status_code=303)
