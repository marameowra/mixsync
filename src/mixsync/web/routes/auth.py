import time
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from mixsync.db.auth import authenticate, create_session, delete_session
from mixsync.web.deps import SESSION_COOKIE, Db
from mixsync.web.templating import templates

router = APIRouter()
SESSION_TTL = timedelta(days=30)

MAX_FAILURES = 10
WINDOW = 15 * 60.0
MAX_KEYS = 10_000
# shortcut: in-process state, fine for the single web process; use a shared store if it scales out.
_failures: dict[tuple[str, str], list[float]] = {}
_now = time.monotonic  # tests replace this to advance time


def _recent(key: tuple[str, str]) -> list[float]:
    cutoff = _now() - WINDOW
    recent = [t for t in _failures.get(key, []) if t > cutoff]
    if recent:
        _failures[key] = recent
    else:
        _failures.pop(key, None)
    return recent


def _record_failure(key: tuple[str, str]) -> None:
    if key not in _failures and len(_failures) >= MAX_KEYS:
        cutoff = _now() - WINDOW
        for k in [k for k, v in _failures.items() if v[-1] <= cutoff]:
            del _failures[k]
        if len(_failures) >= MAX_KEYS:
            del _failures[next(iter(_failures))]
    _failures.setdefault(key, []).append(_now())


@router.get("/login")
def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "pages/login.html", {"error": None})


@router.post("/login")
def login(
    request: Request, db: Db, username: Annotated[str, Form()], password: Annotated[str, Form()]
) -> Response:
    key = (request.client.host if request.client else "", username.lower())
    if len(_recent(key)) >= MAX_FAILURES:
        return templates.TemplateResponse(
            request, "pages/login.html", {"error": "locked"}, status_code=429
        )
    user = authenticate(db, username, password)
    if user is None:
        _record_failure(key)
        return templates.TemplateResponse(
            request, "pages/login.html", {"error": "invalid"}, status_code=401
        )
    _failures.pop(key, None)
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
