import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from mixsync.acquire import lifecycle, recheck
from mixsync.core.capabilities import Capability
from mixsync.db.models.discovery import DiscoveryPlaylist, ProvisionalHold, ProvisionalTrack
from mixsync.db.models.work import Request as AlbumRequest
from mixsync.library import fragmentation
from mixsync.web.deps import CurrentUser, Db, Namer
from mixsync.web.names import resolve_listed
from mixsync.web.templating import templates

router = APIRouter()
FRAGMENTS_TTL = 60.0  # seconds a home page's fragment count may lag a library change


def _fragments(request: Request, db: Db) -> int:
    """len(fragmentation.find), a scan of every verified track, at most once per TTL per app."""
    at, n = getattr(request.app.state, "fragments_count", (float("-inf"), 0))
    if time.monotonic() - at >= FRAGMENTS_TTL:
        n = len(fragmentation.find(db))
        request.app.state.fragments_count = (time.monotonic(), n)
    return n


@router.get("/")
def home(request: Request, user: CurrentUser, db: Db, namer: Namer) -> HTMLResponse:
    counts = request.state.waiting_counts
    promotions = None
    fragments = None
    failed_requests = None
    in_progress: list[AlbumRequest] = []
    recent: list[AlbumRequest] = []
    discovery: list[tuple[ProvisionalHold, ProvisionalTrack, str | None, str | None]] = []
    if Capability.library_edit in user.capabilities:
        promotions = len(recheck.promote_plans(db, "draft"))
        fragments = _fragments(request, db)
    if Capability.request in user.capabilities:
        in_progress = list(
            db.scalars(
                select(AlbumRequest)
                .where(
                    AlbumRequest.user_id == user.id,
                    AlbumRequest.migration_id.is_(None),
                    AlbumRequest.status.in_(lifecycle.IN_PROGRESS),
                )
                .order_by(AlbumRequest.id.desc())
                .limit(4)
            )
        )
        recent = list(
            db.scalars(
                select(AlbumRequest)
                .where(
                    AlbumRequest.user_id == user.id,
                    AlbumRequest.migration_id.is_(None),
                    AlbumRequest.status.in_(("imported", "failed", "no_results")),
                )
                .order_by(AlbumRequest.updated_at.desc(), AlbumRequest.id.desc())
                .limit(3)
            )
        )
        failed_requests = (
            db.scalar(
                select(func.count(AlbumRequest.id)).where(
                    AlbumRequest.user_id == user.id,
                    AlbumRequest.migration_id.is_(None),
                    AlbumRequest.status == "failed",
                )
            )
            or 0
        )
        raw_discovery = db.execute(
            select(ProvisionalHold, ProvisionalTrack, DiscoveryPlaylist.entries)
            .join(ProvisionalTrack, ProvisionalTrack.id == ProvisionalHold.provisional_track_id)
            .join(DiscoveryPlaylist, DiscoveryPlaylist.id == ProvisionalHold.playlist_id)
            .where(
                ProvisionalHold.user_id == user.id,
                ProvisionalHold.expired_at.is_(None),
                ProvisionalTrack.status == "active",
                DiscoveryPlaylist.user_id == user.id,
            )
            .order_by(ProvisionalTrack.imported_at.desc(), ProvisionalTrack.id.desc())
            .limit(5)
        ).all()
        discovery = [
            (
                hold,
                track,
                track.title
                or next(
                    (
                        entry.get("title")
                        for entry in entries
                        if entry.get("recording_mbid") == hold.recording_mbid
                    ),
                    None,
                ),
                track.artist
                or next(
                    (
                        entry.get("artist")
                        for entry in entries
                        if entry.get("recording_mbid") == hold.recording_mbid
                    ),
                    None,
                ),
            )
            for hold, track, entries in raw_discovery
        ]
    refs = {("release", r.release_mbid) for r in (*in_progress, *recent) if r.release_mbid}
    refs |= {("recording", h.recording_mbid) for h, *_ in discovery}
    names = resolve_listed(db, namer, refs)  # list page: stored and cached names, no lookups
    return templates.TemplateResponse(
        request,
        "pages/home.html",
        {
            "names": names,
            "user": user,
            **counts,
            "promotions": promotions,
            "fragments": fragments,
            "failed_requests": failed_requests,
            "in_progress": in_progress,
            "recent": recent,
            "discovery": discovery,
        },
    )
