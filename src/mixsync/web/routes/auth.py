from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from mixsync.db.auth import authenticate, create_session, delete_session
from mixsync.web.deps import SESSION_COOKIE, Db
from mixsync.web.templating import templates

router = APIRouter()
SESSION_TTL = timedelta(days=30)


@router.get("/login")
def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "pages/login.html", {"error": False})


@router.post("/login")
def login(
    request: Request, db: Db, username: Annotated[str, Form()], password: Annotated[str, Form()]
) -> Response:
    user = authenticate(db, username, password)
    if user is None:
        return templates.TemplateResponse(
            request, "pages/login.html", {"error": True}, status_code=401
        )
    token = create_session(
        db, user.id, datetime.now(UTC), SESSION_TTL, request.headers.get("user-agent")
    )
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


@router.post("/logout")
def logout(request: Request, db: Db) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        delete_session(db, token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp
