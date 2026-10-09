from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from mixsync.web.deps import CurrentUser
from mixsync.web.templating import templates

router = APIRouter()


@router.get("/")
def home(request: Request, user: CurrentUser) -> HTMLResponse:
    return templates.TemplateResponse(request, "pages/home.html", {"user": user})
