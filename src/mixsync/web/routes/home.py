from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from mixsync.acquire import recheck
from mixsync.core.capabilities import Capability
from mixsync.web.deps import CurrentUser, Db
from mixsync.web.templating import templates

router = APIRouter()


@router.get("/")
def home(request: Request, user: CurrentUser, db: Db) -> HTMLResponse:
    counts = request.state.waiting_counts
    promotions = None
    if Capability.library_edit in user.capabilities:
        promotions = len(recheck.promote_plans(db, "draft"))
    return templates.TemplateResponse(
        request,
        "pages/home.html",
        {"user": user, **counts, "promotions": promotions},
    )
