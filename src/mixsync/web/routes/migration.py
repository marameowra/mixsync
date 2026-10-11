import csv
import io
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from mixsync.acquire import migration as mig
from mixsync.acquire.requests import LIVE
from mixsync.core.capabilities import Capability
from mixsync.core.jobs import JobKind
from mixsync.db.auth import AuthUser
from mixsync.db.models.work import Job, Migration
from mixsync.db.models.work import Request as AlbumRequest
from mixsync.db.queue import enqueue_in
from mixsync.web.deps import Db, require
from mixsync.web.routes.library import TYPED_ABOVE
from mixsync.web.templating import templates

router = APIRouter(prefix="/migration")
Admin = Annotated[AuthUser, Depends(require(Capability.admin))]

COLUMNS = ("folder", "status", "artist", "album", "release_mbid", "files", "reason")


def _get(db: Db, migration_id: int) -> Migration:
    m = db.get(Migration, migration_id)
    if m is None:
        raise HTTPException(404, "no such migration")
    return m


def _busy(db: Db, m: Migration | None) -> bool:
    """Preflighting, or running with albums still in progress."""
    if m is None:
        return False
    if m.status == "preflight":
        return True
    pending = db.scalar(
        select(AlbumRequest.id)
        .where(
            AlbumRequest.migration_id == m.id, AlbumRequest.status.in_(("verifying", "importing"))
        )
        .limit(1)
    )
    return m.status == "running" and pending is not None


@router.get("")
def page(request: Request, user: Admin, db: Db) -> HTMLResponse:
    m = db.scalar(select(Migration).order_by(Migration.id.desc()).limit(1))
    rows = mig.report(db, m) if m and m.status == "running" else []
    checking = (
        m is not None
        and db.scalar(
            select(Job.id)
            .where(
                Job.kind == JobKind.MIGRATE_CHECK,
                Job.status.in_(LIVE),
                Job.payload["migration_id"].as_integer() == m.id,
            )
            .limit(1)
        )
        is not None
    )
    ctx = {
        "user": user,
        "m": m,
        "busy": _busy(db, m),
        "totals": mig.totals(rows),
        "failures": [r for r in rows if r["status"] == "failed"][:100],
        "checking": checking,
        "typed_needed": bool(m and m.summary.get("albums", 0) > TYPED_ABOVE),
    }
    return templates.TemplateResponse(request, "pages/migration.html", ctx)


@router.post("/preflight")
def preflight(user: Admin, db: Db) -> RedirectResponse:
    latest = db.scalar(select(Migration).order_by(Migration.id.desc()).limit(1))
    if _busy(db, latest):
        raise HTTPException(409, "a migration is already in progress")
    mig.start_preflight(db, datetime.now(UTC), user.id)
    return RedirectResponse("/migration", status_code=303)


@router.post("/{migration_id}/confirm")
def confirm(
    migration_id: int,
    user: Admin,
    db: Db,
    count: Annotated[str, Form()] = "",
    paused: Annotated[str, Form()] = "",
) -> RedirectResponse:
    m = _get(db, migration_id)
    if m.status == "running":  # a second confirm does nothing
        return RedirectResponse("/migration", status_code=303)
    if m.status != "ready":
        raise HTTPException(409, f"migration is {m.status}")
    if not paused:
        raise HTTPException(400, "confirm that other MusicBrainz clients are paused")
    albums = m.summary["albums"]
    if albums > TYPED_ABOVE and count.strip() != str(albums):
        raise HTTPException(400, f"type {albums} to confirm")
    mig.confirm(db, datetime.now(UTC), m)
    return RedirectResponse("/migration", status_code=303)


@router.post("/{migration_id}/check")
def check(migration_id: int, user: Admin, db: Db) -> RedirectResponse:
    m = _get(db, migration_id)
    if not m.manifest_path:
        raise HTTPException(409, "there is no manifest yet")
    enqueue_in(db, datetime.now(UTC), JobKind.MIGRATE_CHECK, {"migration_id": m.id})
    return RedirectResponse("/migration", status_code=303)


@router.get("/{migration_id}/report.csv")
def report_csv(migration_id: int, user: Admin, db: Db) -> Response:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(COLUMNS)
    w.writerows([r[c] for c in COLUMNS] for r in mig.report(db, _get(db, migration_id)))
    return Response(
        out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="migration-{migration_id}.csv"'},
    )
