from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from mixsync.core.capabilities import Capability
from mixsync.db.auth import AuthUser
from mixsync.web.deps import Db, decision_for, require

router = APIRouter(prefix="/media")
User = Annotated[AuthUser, Depends(require(Capability.request))]


@router.get("/preview/{decision_id}/{index}")
def preview(request: Request, decision_id: int, index: int, user: User, db: Db) -> FileResponse:
    d = decision_for(db, user, decision_id)
    files = d.evidence.get("source", {}).get("files", [])
    if not 0 <= index < len(files):
        raise HTTPException(404)
    settings = request.app.state.settings
    roots = [Path(settings.downloads_dir).resolve(), Path(settings.data_dir).resolve()]
    path = Path(str(files[index].get("local_path", ""))).resolve()  # follows symlinks and ..
    if not any(path.is_relative_to(r) for r in roots) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)  # Starlette answers Range requests with 206
