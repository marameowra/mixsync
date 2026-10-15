from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

from mixsync.core.capabilities import Capability
from mixsync.core.errors import PermanentError, TransientError
from mixsync.db.auth import AuthUser
from mixsync.web.deps import Cover, Db, decision_for, require

router = APIRouter(prefix="/media")
User = Annotated[AuthUser, Depends(require(Capability.request))]


@router.get("/preview/{decision_id}/{index}")
def preview(request: Request, decision_id: int, index: int, user: User, db: Db) -> FileResponse:
    d = decision_for(db, user, decision_id)
    files = d.evidence.get("source", {}).get("files", [])
    if not 0 <= index < len(files):
        raise HTTPException(404)
    settings = request.app.state.settings
    roots = [
        Path(d).resolve()
        for d in (settings.downloads_dir, settings.data_dir, settings.import_source_dir)
    ]
    path = Path(str(files[index].get("local_path", ""))).resolve()  # follows symlinks and ..
    if not any(path.is_relative_to(r) for r in roots) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)  # Starlette answers Range requests with 206


@router.get("/cover/{decision_id}")
async def cover(decision_id: int, user: User, db: Db, art: Cover) -> Response:
    """The candidate release's front cover, fetched server-side through the polite client."""
    d = decision_for(db, user, decision_id)
    mbid = d.release_mbid
    group = d.evidence.get("target", {}).get("release_group_id")
    db.commit()  # the login's last_seen write must not hold the SQLite lock during the bucket's
    headers = {"Cache-Control": "private, max-age=86400"}
    try:
        data = await art.front(mbid, group) if mbid else None
    except (TransientError, PermanentError):
        data = None
    if data is None:
        raise HTTPException(404, headers=headers)
    kind = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
    return Response(data, media_type=kind, headers=headers)
