import csv
import io
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import CursorResult, select, update

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


def _latest(db: Db) -> Migration | None:
    return db.scalar(select(Migration).order_by(Migration.id.desc()).limit(1))


def _busy(db: Db, m: Migration | None) -> bool:
    """Preflighting, waiting for confirmation, or running with albums not yet settled."""
    if m is None:
        return False
    if m.status in ("preflight", "ready"):
        return True
    pending = db.scalar(
        select(AlbumRequest.id)
        .where(
            AlbumRequest.migration_id == m.id,
            AlbumRequest.status.in_(("verifying", "importing", "review")),
        )
        .limit(1)
    )
    return m.status == "running" and pending is not None


def _checking(db: Db, m: Migration) -> bool:
    return (
        db.scalar(
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


def _preflight_live(db: Db, m: Migration) -> bool:
    return (
        db.scalar(
            select(Job.id)
            .where(
                Job.kind == JobKind.MIGRATE_PREFLIGHT,
                Job.status.in_(LIVE),
                Job.payload["migration_id"].as_integer() == m.id,
            )
            .limit(1)
        )
        is not None
    )


@router.get("")
def page(request: Request, user: Admin, db: Db) -> HTMLResponse:
    latest = _latest(db)
    running = db.scalars(
        select(Migration).where(Migration.status == "running").order_by(Migration.id.desc())
    ).all()
    shown = [latest, *running] if latest and latest.status != "running" else list(running)
    entries: list[dict[str, Any]] = []
    for m in shown:
        rows = mig.report(db, m) if m.status == "running" else []
        entries.append(
            {
                "m": m,
                "totals": mig.totals(rows),
                "failures": [r for r in rows if r["status"] in ("failed", "skipped")],
                "checking": _checking(db, m),
                "preflight_live": _preflight_live(db, m),
                "typed_needed": m.summary.get("albums", 0) > TYPED_ABOVE,
            }
        )
    ctx = {"user": user, "migs": entries, "busy": _busy(db, latest)}
    return templates.TemplateResponse(request, "pages/migration.html", ctx)


@router.post("/preflight")
def preflight(user: Admin, db: Db) -> RedirectResponse:
    latest = _latest(db)
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
    if m.status != "ready" or _latest(db) is not m:
        raise HTTPException(409, f"migration is {m.status}")
    if not paused:
        raise HTTPException(400, "confirm that other MusicBrainz clients are paused")
    albums = m.summary["albums"]
    if albums > TYPED_ABOVE and count.strip() != str(albums):
        raise HTTPException(400, f"type {albums} to confirm")
    mig.confirm(db, datetime.now(UTC), m)
    return RedirectResponse("/migration", status_code=303)


@router.post("/{migration_id}/discard")
def discard(migration_id: int, user: Admin, db: Db) -> RedirectResponse:
    """Drop an unconfirmed preflight (or one whose job died) so a new one can start."""
    m = _get(db, migration_id)
    ok = ("ready",) if _preflight_live(db, m) else ("ready", "preflight")
    done = cast(
        CursorResult[Any],
        db.execute(
            update(Migration)
            .where(Migration.id == m.id, Migration.status.in_(ok))
            .values(status="cancelled")
        ),
    )
    if done.rowcount != 1:
        raise HTTPException(409, f"migration is {m.status}")
    return RedirectResponse("/migration", status_code=303)


@router.post("/{migration_id}/retry/{request_id}")
def retry(migration_id: int, request_id: int, user: Admin, db: Db) -> RedirectResponse:
    req = db.get(AlbumRequest, request_id)
    if req is None or req.migration_id != _get(db, migration_id).id:
        raise HTTPException(404, "no such album in this migration")
    if why := mig.retry(db, datetime.now(UTC), req):
        raise HTTPException(409, f"cannot retry: {why}")
    return RedirectResponse("/migration", status_code=303)


@router.post("/{migration_id}/check")
def check(migration_id: int, user: Admin, db: Db) -> RedirectResponse:
    m = _get(db, migration_id)
    if not m.manifest_path:
        raise HTTPException(409, "there is no manifest yet")
    if _checking(db, m):
        raise HTTPException(409, "a check is already running")
    enqueue_in(db, datetime.now(UTC), JobKind.MIGRATE_CHECK, {"migration_id": m.id})
    return RedirectResponse("/migration", status_code=303)


def _safe(v: Any) -> Any:
    """A spreadsheet runs a cell that starts like a formula; tags and file names are untrusted."""
    return "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v


@router.get("/{migration_id}/report.csv")
def report_csv(migration_id: int, user: Admin, db: Db) -> Response:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(COLUMNS)
    w.writerows([_safe(r[c]) for c in COLUMNS] for r in mig.report(db, _get(db, migration_id)))
    return Response(
        out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="migration-{migration_id}.csv"'},
    )
