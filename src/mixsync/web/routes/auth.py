import threading
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
_lock = threading.Lock()
_now = time.monotonic  # tests replace this to advance time


def admit(*keys: tuple[str, str]) -> bool:
    """Prune, check and record the attempt on every key in one step so parallel requests can't
    all slip past. If any key is full nothing is recorded."""
    with _lock:
        now = _now()
        recent = {k: [t for t in _failures.get(k, []) if t > now - WINDOW] for k in keys}
        if any(len(r) >= MAX_FAILURES for r in recent.values()):
            for k, r in recent.items():
                if r:
                    _failures[k] = r
                else:
                    _failures.pop(k, None)
            return False
        for key, r in recent.items():
            if key not in _failures and len(_failures) >= MAX_KEYS:
                for k in [k for k, v in _failures.items() if not v or v[-1] <= now - WINDOW]:
                    del _failures[k]
                if len(_failures) >= MAX_KEYS:  # oldest first, but never this call's own keys
                    del _failures[next(k for k in _failures if k not in keys)]
            _failures[key] = [*r, now]
        return True


def refund(*keys: tuple[str, str]) -> None:
    """Take back the newest attempt on each key (it was not a failed guess)."""
    with _lock:
        for k in keys:
            if _failures.get(k):
                _failures[k].pop()
            if not _failures.get(k, True):
                del _failures[k]


def forget(*keys: tuple[str, str]) -> None:
    with _lock:
        for k in keys:
            _failures.pop(k, None)


@router.get("/login")
def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "pages/login.html", {"error": None})


@router.post("/login")
def login(
    request: Request, db: Db, username: Annotated[str, Form()], password: Annotated[str, Form()]
) -> Response:
    key = (request.client.host if request.client else "", username.lower())
    if not admit(key):
        return templates.TemplateResponse(
            request, "pages/login.html", {"error": "locked"}, status_code=429
        )
    user = authenticate(db, username, password)
    if user is None:
        return templates.TemplateResponse(
            request, "pages/login.html", {"error": "invalid"}, status_code=401
        )
    forget(key)
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
