from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import ColumnElement, delete, select
from sqlalchemy.orm import Session

from mixsync.acquire.requests import create_request
from mixsync.core.capabilities import Capability
from mixsync.core.jobs import JobState
from mixsync.db import decisions
from mixsync.db.auth import AuthUser
from mixsync.db.models.matching import MatchDecision
from mixsync.db.models.work import Job
from mixsync.db.models.work import Request as AlbumRequest
from mixsync.web.deps import Db, require
from mixsync.web.routes.review import parse_release_mbid
from mixsync.web.templating import templates

router = APIRouter(prefix="/requests")
User = Annotated[AuthUser, Depends(require(Capability.request))]


_BUSY = ("downloading", "verifying", "importing")
_LIVE = (JobState.QUEUED, JobState.LEASED, JobState.RUNNING, JobState.RETRY_WAIT)


def _of(req_id: int) -> ColumnElement[bool]:
    return Job.payload["request_id"].as_integer() == req_id


def _live_job(db: Session, req: AlbumRequest) -> bool:
    return db.scalar(select(Job.id).where(_of(req.id), Job.status.in_(_LIVE)).limit(1)) is not None


@router.get("")
def request_list(request: Request, user: User, db: Db) -> HTMLResponse:
    items = db.scalars(
        select(AlbumRequest).where(AlbumRequest.user_id == user.id).order_by(AlbumRequest.id.desc())
    ).all()
    deletable = {r.id for r in items if r.status not in _BUSY and not _live_job(db, r)}
    return templates.TemplateResponse(
        request, "pages/requests.html", {"user": user, "items": items, "deletable": deletable}
    )


@router.post("")
def new_request(user: User, db: Db, release: Annotated[str, Form()]) -> RedirectResponse:
    create_request(db, datetime.now(UTC), user.id, parse_release_mbid(release))
    return RedirectResponse("/requests", status_code=303)


@router.post("/{request_id}/delete")
def delete_request(request_id: int, user: User, db: Db) -> RedirectResponse:
    """Drops the row and its finished jobs; files on disk and decisions (the audit record) stay."""
    req = db.get(AlbumRequest, request_id)
    if req is None or req.user_id != user.id:
        raise HTTPException(403 if req else 404, "not your request")
    if req.status in _BUSY or _live_job(db, req):
        raise HTTPException(409, "request is in progress")
    # A decision still pending for this request would sit in /review with nothing to act on.
    for d in db.scalars(select(MatchDecision).where(MatchDecision.status == decisions.PENDING)):
        if d.evidence.get("request_id") == req.id:
            decisions.act(db, d.id, user.id, "reject")
    db.execute(delete(Job).where(_of(req.id)))
    db.delete(req)
    return RedirectResponse("/requests", status_code=303)
