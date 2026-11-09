"""Trash usage per day folder, and the early purge: dry-run plan, then a typed confirmation."""

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from mixsync.core.capabilities import Capability
from mixsync.db.auth import AuthUser
from mixsync.library.purge import TrashFolder, TrashPurger
from mixsync.web.deps import Db, require
from mixsync.web.templating import templates

router = APIRouter(prefix="/trash")
Admin = Annotated[AuthUser, Depends(require(Capability.admin))]


def _purger(request: Request) -> TrashPurger:
    s = request.app.state.services
    return TrashPurger(s.session_factory, s.clock, s.fileops, request.app.state.settings.trash_days)


@router.get("")
def show(
    request: Request,
    admin: Admin,
    purged: int | None = None,
    skipped: Annotated[list[str], Query()] = [],  # noqa: B006
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "pages/trash.html",
        {
            "user": admin,
            "folders": _purger(request).folders(),
            "purged": purged,
            "skipped": skipped,
            "trash_days": request.app.state.settings.trash_days,
        },
    )


@router.get("/plan")
def plan(
    request: Request,
    admin: Admin,
    day: Annotated[list[str], Query()] = [],  # noqa: B006
) -> HTMLResponse:
    """The dry run: only chosen folders that exist and are not protected."""
    chosen: list[TrashFolder] = [
        f for f in _purger(request).folders() if f.day in day and not f.protected
    ]
    if not chosen:
        raise HTTPException(400, "choose at least one unprotected trash folder")
    return templates.TemplateResponse(
        request,
        "pages/trash_plan.html",
        {
            "user": admin,
            "chosen": chosen,
            "files": sum(f.files for f in chosen),
            "size": sum(f.size for f in chosen),
        },
    )


@router.post("/purge")
def purge(
    request: Request,
    admin: Admin,
    db: Db,
    day: Annotated[list[str], Form()],
    count: Annotated[str, Form()] = "",
) -> RedirectResponse:
    purger = _purger(request)
    usage = {f.day: f for f in purger.folders()}
    if unknown := set(day) - usage.keys():
        raise HTTPException(400, f"no such trash folder: {', '.join(sorted(unknown))}")
    # Counted over every chosen folder, protected or not, so a folder that became protected
    # since the plan is skipped and reported rather than turning the confirmation into a 400.
    files = sum(usage[d].files for d in set(day))
    if count.strip() != str(files):
        raise HTTPException(400, f"type {files} to confirm")
    db.commit()  # free the SQLite write lock (login last_seen) for the journal writes
    purged, skipped = purger.purge_early(day)
    query = urlencode({"purged": purged, "skipped": skipped}, doseq=True)
    return RedirectResponse(f"/trash?{query}", status_code=303)
