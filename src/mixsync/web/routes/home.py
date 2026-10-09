from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from mixsync.core.capabilities import Capability
from mixsync.db import decisions
from mixsync.web.deps import CurrentUser, Db
from mixsync.web.templating import templates

router = APIRouter()


@router.get("/")
def home(request: Request, user: CurrentUser, db: Db) -> HTMLResponse:
    owner = None if Capability.approve in user.capabilities else user.id
    pending = len(decisions.pending_for(db, owner))
    return templates.TemplateResponse(
        request, "pages/home.html", {"user": user, "pending": pending}
    )
