from datetime import UTC, datetime
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from mixsync.core.errors import PermanentError, TransientError
from mixsync.db.models.accounts import UserLink
from mixsync.ratelimit.client import polite_client
from mixsync.targets.subsonic import SubsonicClient
from mixsync.web.deps import CurrentUser, Db
from mixsync.web.templating import templates

router = APIRouter(prefix="/account")
SERVICES = ("listenbrainz", "navidrome")


class LinkValidator(Protocol):
    async def listenbrainz_username(self, token: str) -> str | None:
        """The username the token belongs to, or None if ListenBrainz rejects it."""
        ...

    async def navidrome_ping(self, username: str, password: str) -> bool: ...


class DefaultValidator:
    def __init__(self, request: Request) -> None:
        self._state = request.app.state

    # SEAM: the real ListenBrainz client (GET /1/validate-token) arrives in a later slice.
    # Until then no token validates, so nothing unverified is ever stored. Replace this method,
    # or set app.state.link_validator.
    async def listenbrainz_username(self, token: str) -> str | None:
        return None

    async def navidrome_ping(self, username: str, password: str) -> bool:
        st = self._state
        client = polite_client("navidrome", st.settings, st.session_factory)
        try:
            await SubsonicClient(client, st.settings.navidrome_url, username, password).ping()
        except (PermanentError, TransientError):
            return False  # the exception text is not shown: it could echo the credentials
        return True


def get_validator(request: Request) -> LinkValidator:
    v: LinkValidator | None = getattr(request.app.state, "link_validator", None)
    return v or DefaultValidator(request)


Validator = Annotated[LinkValidator, Depends(get_validator)]


def get_link(s: Session, user_id: int, service: str) -> UserLink | None:
    return s.scalar(
        select(UserLink).where(UserLink.user_id == user_id, UserLink.service == service)
    )


def _service(service: str) -> str:
    if service not in SERVICES:
        raise HTTPException(404, "no such service")
    return service


def _page(
    request: Request, user: CurrentUser, db: Db, error: tuple[str, str] | None = None
) -> HTMLResponse:
    links = {sv: get_link(db, user.id, sv) for sv in SERVICES}
    ctx = {"user": user, "links": links, "error": error}
    return templates.TemplateResponse(
        request, "pages/account.html", ctx, status_code=400 if error else 200
    )


@router.get("")
def show(request: Request, user: CurrentUser, db: Db) -> HTMLResponse:
    return _page(request, user, db)


@router.post("/{service}")
async def save(
    request: Request,
    service: str,
    user: CurrentUser,
    db: Db,
    validator: Validator,
    secret: Annotated[str, Form()],
    username: Annotated[str, Form()] = "",
) -> Response:
    _service(service)
    secret, username = secret.strip(), username.strip()
    db.commit()  # don't hold the SQLite write lock across the outbound call
    if service == "listenbrainz":
        name = (await validator.listenbrainz_username(secret) if secret else None) or ""
        msg = "ListenBrainz did not accept that token."
    else:
        name = username
        ok = bool(name and secret) and await validator.navidrome_ping(name, secret)
        name = name if ok else ""
        msg = "Navidrome did not accept that username and password."
    with request.app.state.session_factory.begin() as s:  # db is spent after the commit above
        if not name:
            return _page(request, user, s, (service, msg))
        link = get_link(s, user.id, service)
        if link is None:
            link = UserLink(user_id=user.id, service=service)
            s.add(link)
        link.username, link.secret = name, secret
        link.verified_at = datetime.now(UTC)
    return RedirectResponse("/account", 303)


@router.post("/{service}/unlink")
def unlink(service: str, user: CurrentUser, db: Db) -> RedirectResponse:
    link = get_link(db, user.id, _service(service))
    if link is not None:
        db.delete(link)
    return RedirectResponse("/account", 303)
