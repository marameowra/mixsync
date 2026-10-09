import hashlib
import hmac

from fastapi import HTTPException, Request

from mixsync.web.deps import SESSION_COOKIE

_LABEL = b"mixsync-csrf-v1"


def csrf_token(request: Request) -> str:
    """HMAC of the raw session cookie: unguessable without the cookie, nothing to store, and it
    changes whenever the session rotates. Empty when there is no session."""
    cookie = request.cookies.get(SESSION_COOKIE)
    return hmac.new(_LABEL, cookie.encode(), hashlib.sha256).hexdigest() if cookie else ""


async def check_csrf(request: Request) -> None:
    """App-wide dependency: every non-GET except POST /login must carry the token."""
    if request.method in ("GET", "HEAD", "OPTIONS") or request.url.path == "/login":
        return
    sent = request.headers.get("X-CSRF-Token")
    if sent is None:
        sent = str((await request.form()).get("csrf_token", ""))
    expected = csrf_token(request)
    if not expected or not hmac.compare_digest(sent.encode(), expected.encode()):
        raise HTTPException(403, "invalid CSRF token")
