from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mixsync.core.errors import PermanentError, TransientError
from mixsync.db.auth import AuthUser
from mixsync.db.models.accounts import UserLink
from mixsync.db.secretbox import SecretBox, set_secret, user_links
from mixsync.discovery import playlists
from mixsync.discovery.listenbrainz import ListenBrainzClient
from mixsync.ratelimit.client import PoliteClient
from mixsync.targets.subsonic import SubsonicClient
from mixsync.web.deps import (
    Box,
    CurrentUser,
    Db,
    get_listenbrainz_http,
    get_navidrome_http,
)
from mixsync.web.templating import templates

router = APIRouter(prefix="/account")
SERVICES = ("listenbrainz", "navidrome")


class Ping(StrEnum):
    ok = "ok"
    rejected = "rejected"
    unreachable = "unreachable"


class LinkValidator(Protocol):
    async def listenbrainz_username(self, token: str) -> str | None:
        """The username the token belongs to, or None if ListenBrainz rejects it. Raises
        TransientError when ListenBrainz can't be reached."""
        ...

    async def navidrome_ping(self, username: str, password: str) -> Ping: ...


class DefaultValidator:
    def __init__(self, http: PoliteClient, navidrome_url: str, lb: ListenBrainzClient) -> None:
        self._http, self._url, self._lb = http, navidrome_url, lb

    async def listenbrainz_username(self, token: str) -> str | None:
        try:
            return await self._lb.validate_token(token)
        except PermanentError:
            return None  # the exception text is not shown

    async def navidrome_ping(self, username: str, password: str) -> Ping:
        try:
            await SubsonicClient(self._http, self._url, username, password).ping()
        except TransientError:
            return Ping.unreachable
        except PermanentError:
            return Ping.rejected  # the exception text is not shown: it could echo credentials
        return Ping.ok


def get_validator(
    request: Request,
    http: Annotated[PoliteClient, Depends(get_navidrome_http)],
    lb_http: Annotated[PoliteClient, Depends(get_listenbrainz_http)],
) -> LinkValidator:
    v: LinkValidator | None = getattr(request.app.state, "link_validator", None)
    return v or DefaultValidator(
        http, request.app.state.settings.navidrome_url, ListenBrainzClient(lb_http)
    )


Validator = Annotated[LinkValidator, Depends(get_validator)]
MAX_LEN = 200  # far above any real token, password or username


def get_link(s: Session, user_id: int, service: str) -> UserLink | None:
    return s.scalar(
        select(UserLink).where(UserLink.user_id == user_id, UserLink.service == service)
    )


def _service(service: str) -> str:
    if service not in SERVICES:
        raise HTTPException(404, "no such service")
    return service


def _page(
    request: Request,
    user: AuthUser,
    s: Session,
    box: SecretBox,
    error: tuple[str, str] | None = None,
) -> HTMLResponse:
    by_service = {link.service: link for link in user_links(s, box, user.id)}
    ctx = {"user": user, "links": by_service, "error": error}
    return templates.TemplateResponse(
        request, "pages/account.html", ctx, status_code=400 if error else 200
    )


@router.get("")
def show(request: Request, user: CurrentUser, db: Db, box: Box) -> HTMLResponse:
    return _page(request, user, db, box)


@router.post("/{service}")
async def save(
    request: Request,
    service: str,
    user: CurrentUser,
    db: Db,
    box: Box,
    validator: Validator,
    secret: Annotated[str, Form()],
    username: Annotated[str, Form()] = "",
) -> Response:
    _service(service)
    # shortcut: no attempt limit on Navidrome logins; a logged-in user can guess their own
    # Navidrome password via this form. Accepted: Navidrome rate-limits itself; add one if abused.
    # a Navidrome password may legitimately start or end with spaces; the rest are identifiers
    secret = secret if service == "navidrome" else secret.strip()
    username = username.strip()
    db.commit()  # don't hold the SQLite write lock across the outbound call
    name, msg = "", "That is too long."
    if len(secret) <= MAX_LEN and len(username) <= MAX_LEN:
        if service == "listenbrainz":
            msg = "ListenBrainz did not accept that token."
            try:
                name = (await validator.listenbrainz_username(secret) if secret else None) or ""
            except TransientError:
                name, msg = "", "Couldn't reach ListenBrainz; try again."
        else:
            name, msg = "", "Navidrome did not accept that username and password."
            if username and secret:
                match await validator.navidrome_ping(username, secret):
                    case Ping.ok:
                        name = username
                    case Ping.unreachable:
                        msg = "Couldn't reach Navidrome; try again."
                    case Ping.rejected:
                        pass
    for attempt in (1, 2):  # a double submit can lose the insert race; retry as an update
        try:
            with request.app.state.session_factory.begin() as s:  # db is spent after the commit
                if not name:
                    return _page(request, user, s, box, (service, msg))
                link = get_link(s, user.id, service)
                if link is None:
                    link = UserLink(user_id=user.id, service=service)
                    s.add(link)
                link.external_username = name
                set_secret(box, link, secret)
                link.verified_at = datetime.now(UTC)
                if service == "listenbrainz":
                    playlists.ensure_default(s, datetime.now(UTC), user.id)
            break
        except IntegrityError:
            if attempt == 2:
                raise
    return RedirectResponse("/account", 303)


@router.post("/{service}/unlink")
def unlink(service: str, user: CurrentUser, db: Db) -> RedirectResponse:
    link = get_link(db, user.id, _service(service))
    if link is not None:
        db.delete(link)
    return RedirectResponse("/account", 303)
