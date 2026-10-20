from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from mixsync.acquire.lifecycle import NO_FILE
from mixsync.core.capabilities import Capability
from mixsync.core.clock import aware
from mixsync.db.auth import AuthUser
from mixsync.db.models.accounts import UserLink
from mixsync.db.models.discovery import DiscoveryPlaylist, ProvisionalHold, ProvisionalTrack
from mixsync.db.models.library import Track
from mixsync.db.models.work import Request as WorkRequest
from mixsync.discovery import playlists, retention
from mixsync.web.deps import Db, require
from mixsync.web.templating import templates

router = APIRouter(prefix="/discover")
User = Annotated[AuthUser, Depends(require(Capability.request))]


def _own(db: Db, user: AuthUser, playlist_id: int) -> DiscoveryPlaylist:
    pl = db.get(DiscoveryPlaylist, playlist_id)
    if pl is None:
        raise HTTPException(404, "no such playlist")
    if pl.user_id != user.id:
        raise HTTPException(403, "not your playlist")
    return pl


def _status(h: ProvisionalHold | None, owned: bool, request_status: str | None) -> str:
    if h is not None and h.kept_at is not None:
        return "kept"
    if owned:
        return "owned"
    if h is None:
        return "-"
    if h.expired_at is not None:
        return "expired"
    if h.provisional_track_id:
        return "in discover"
    # a request that ended without a file (lifecycle.NO_FILE) found nothing
    return "not found" if request_status in NO_FILE else "waiting for download"


def _artwork_src(release: str | None, group: str | None) -> str | None:
    if release:
        return f"/media/artwork/release/{release}" + (f"?group={group}" if group else "")
    return f"/media/artwork/release-group/{group}" if group else None


@router.get("")
def playlist_list(request: Request, user: User, db: Db) -> HTMLResponse:
    rows = db.scalars(
        select(DiscoveryPlaylist)
        .where(DiscoveryPlaylist.user_id == user.id)
        .order_by(DiscoveryPlaylist.id)
    ).all()
    linked = set(db.scalars(select(UserLink.service).where(UserLink.user_id == user.id)))
    holds = {
        h.recording_mbid: h
        for h in db.scalars(select(ProvisionalHold).where(ProvisionalHold.user_id == user.id))
    }
    req_ids = [h.request_id for h in holds.values() if h.request_id]
    requests = list(db.scalars(select(WorkRequest).where(WorkRequest.id.in_(req_ids))))
    req_by_id = {r.id: r for r in requests}
    req_status = {r.id: r.status for r in requests}
    now = datetime.now(UTC)
    due = retention.expiring_soon(db, now, user.id)
    mbids = [e["recording_mbid"] for p in rows for e in p.entries]
    mbids.extend(h.recording_mbid for h in due)
    artwork_ids: dict[str, tuple[str | None, str | None]] = {}
    owned_tracks = list(db.scalars(select(Track).where(Track.recording_mbid.in_(mbids))))
    for track in owned_tracks:
        if track.recording_mbid and (track.release_mbid or track.release_group_mbid):
            artwork_ids.setdefault(
                track.recording_mbid, (track.release_mbid, track.release_group_mbid)
            )
    for track in db.scalars(
        select(ProvisionalTrack).where(ProvisionalTrack.recording_mbid.in_(mbids))
    ):
        if track.release_mbid or track.release_group_mbid:
            artwork_ids.setdefault(
                track.recording_mbid, (track.release_mbid, track.release_group_mbid)
            )
    for hold in holds.values():
        if req := req_by_id.get(hold.request_id or 0):
            if req.release_mbid or req.release_group_mbid:
                for recording in req.recording_mbids:
                    artwork_ids.setdefault(recording, (req.release_mbid, req.release_group_mbid))
    owned = {track.recording_mbid for track in owned_tracks}
    tracks: dict[int, list[dict[str, Any]]] = {}
    for p in rows:
        tracks[p.id] = []
        for e in p.entries:
            m = e["recording_mbid"]
            h = holds.get(m)
            rs = req_status.get(h.request_id) if h and h.request_id else None
            release, group = artwork_ids.get(m, (None, None))
            tracks[p.id].append(
                {
                    **e,
                    "status": _status(h, m in owned, rs),
                    "hold": h,
                    "artwork_src": _artwork_src(release, group),
                }
            )
    pts = {
        t.id: t
        for t in db.scalars(
            select(ProvisionalTrack).where(
                ProvisionalTrack.id.in_(
                    [h.provisional_track_id for h in due if h.provisional_track_id]
                )
            )
        )
    }
    names = {e["recording_mbid"]: e for p in rows for e in p.entries}  # old rows lack a title
    soon: list[dict[str, Any]] = []
    for h in due:
        t, e = pts.get(h.provisional_track_id or 0), names.get(h.recording_mbid, {})
        title = (t and t.title) or e.get("title") or h.recording_mbid
        artist = (t and t.artist) or e.get("artist")
        soon.append(
            {
                "mbid": h.recording_mbid,
                "name": f"{artist} - {title}" if artist else title,
                "artwork_src": _artwork_src(*artwork_ids.get(h.recording_mbid, (None, None))),
                "expires": h.expires_at,
                "overdue": bool(h.expires_at and aware(h.expires_at) <= now),
            }
        )
    ctx: dict[str, Any] = {
        "user": user,
        "rows": rows,
        "tracks": tracks,
        "soon": soon,
        "linked": {"listenbrainz", "navidrome"} <= linked,
    }
    return templates.TemplateResponse(request, "pages/discover.html", ctx)


@router.post("/keep/{recording_mbid}")
def keep(recording_mbid: str, user: User, db: Db) -> RedirectResponse:
    # keep() looks only at this user's holds, so another user's track is just "not found"
    if not retention.keep(db, datetime.now(UTC), user.id, recording_mbid):
        raise HTTPException(404, "no such track in your discovery")
    return RedirectResponse("/discover", status_code=303)


def _whole(value: str, name: str, high: int) -> int:
    v = value.strip()
    if not (v.isascii() and v.isdigit() and 1 <= int(v) <= high):
        raise HTTPException(400, f"{name} is a whole number from 1 to {high}")
    return int(v)


@router.post("/{playlist_id}/settings")
def settings(
    playlist_id: int,
    user: User,
    db: Db,
    size: Annotated[str, Form()] = "",  # empty fields reach our 400, not FastAPI's 422
    unowned_ratio: Annotated[str, Form()] = "",
    cadence_days: Annotated[str, Form()] = "",
    retention_days: Annotated[str, Form()] = "",
    keep_plays: Annotated[str, Form()] = "",
) -> RedirectResponse:
    pl = _own(db, user, playlist_id)
    try:
        ratio = float(unowned_ratio)
    except ValueError:
        ratio = -1.0
    if not 0 <= ratio <= 1:  # also rejects nan
        raise HTTPException(400, "unowned ratio is a number from 0 to 1")
    pl.size = _whole(size, "size", 500)
    pl.unowned_ratio = ratio
    pl.cadence_days = _whole(cadence_days, "cadence", 365)
    pl.retention_days = _whole(retention_days, "retention", 365)
    pl.keep_plays = _whole(keep_plays, "keep plays", 1000)
    return RedirectResponse("/discover", status_code=303)


@router.post("/{playlist_id}/build")
def build_now(playlist_id: int, user: User, db: Db) -> RedirectResponse:
    pl = _own(db, user, playlist_id)
    if not playlists.build_now(db, datetime.now(UTC), pl):
        raise HTTPException(409, "built within the last hour")
    return RedirectResponse("/discover", status_code=303)
