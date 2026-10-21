from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from mixsync.acquire import lifecycle, recheck
from mixsync.core.capabilities import Capability
from mixsync.db.models.discovery import DiscoveryPlaylist, ProvisionalHold, ProvisionalTrack
from mixsync.db.models.work import Request as AlbumRequest
from mixsync.library import fragmentation
from mixsync.web.deps import CurrentUser, Db
from mixsync.web.templating import templates

router = APIRouter()


@router.get("/")
def home(request: Request, user: CurrentUser, db: Db) -> HTMLResponse:
    counts = request.state.waiting_counts
    promotions = None
    fragments = None
    failed_requests = None
    in_progress: list[AlbumRequest] = []
    recent: list[AlbumRequest] = []
    discovery: list[tuple[ProvisionalHold, ProvisionalTrack, str | None, str | None]] = []
    if Capability.library_edit in user.capabilities:
        promotions = len(recheck.promote_plans(db, "draft"))
        fragments = len(fragmentation.find(db))
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
    return templates.TemplateResponse(
        request,
        "pages/home.html",
        {
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
