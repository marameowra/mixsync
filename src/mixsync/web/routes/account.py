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
from mixsync.metadata.discogs import DiscogsClient
from mixsync.ratelimit.client import PoliteClient
from mixsync.targets.subsonic import SubsonicClient
from mixsync.web.deps import (
    Box,
    CurrentUser,
    Db,
    get_discogs,
    get_listenbrainz_http,
    get_navidrome_http,
)
from mixsync.web.routes.auth import admit, forget, refund
from mixsync.web.templating import templates

router = APIRouter(prefix="/account")
SERVICES = ("listenbrainz", "navidrome", "discogs")


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

    async def discogs_username(self, token: str) -> str | None:
        """The username the token belongs to, or None if Discogs rejects it. Raises
        TransientError when Discogs can't be reached."""
        ...


class DefaultValidator:
    def __init__(
        self, http: PoliteClient, navidrome_url: str, lb: ListenBrainzClient, discogs: DiscogsClient
    ) -> None:
        self._http, self._url, self._lb, self._discogs = http, navidrome_url, lb, discogs

    async def discogs_username(self, token: str) -> str | None:
        return await self._discogs.username(token)  # rejection is None; the text is not shown

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
    discogs: Annotated[DiscogsClient, Depends(get_discogs)],
) -> LinkValidator:
    v: LinkValidator | None = getattr(request.app.state, "link_validator", None)
    return v or DefaultValidator(
        http, request.app.state.settings.navidrome_url, ListenBrainzClient(lb_http), discogs
    )


Validator = Annotated[LinkValidator, Depends(get_validator)]
MAX_LEN = 200  # far above any real token, password or username


def get_link(s: Session, user_id: int, service: str) -> UserLink | None:
    return s.scalar(
        select(UserLink).where(UserLink.user_id == user_id, UserLink.service == service)
    )


def _service(request: Request, service: str, *, linking: bool = True) -> str:
    """`linking` False is for removing a stored link, which stays possible with Discogs off."""
    if service not in SERVICES or (
        linking and service == "discogs" and not request.app.state.settings.discogs_suggestions
    ):
        raise HTTPException(404, "no such service")
    return service


def _page(
    request: Request,
    user: AuthUser,
    s: Session,
    box: SecretBox,
    error: tuple[str, str] | None = None,
    status: int = 400,
) -> HTMLResponse:
    discogs = request.app.state.settings.discogs_suggestions
    by_service = {
        link.service: link
        for link in user_links(s, box, user.id)
        if discogs or link.service != "discogs"
    }
    ctx = {"user": user, "links": by_service, "error": error, "discogs": discogs}
    return templates.TemplateResponse(
        request, "pages/account.html", ctx, status_code=status if error else 200
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
    _service(request, service)
    # a Navidrome password may legitimately start or end with spaces; the rest are identifiers
    secret = secret if service == "navidrome" else secret.strip()
    username = username.strip()
    db.commit()  # don't hold the SQLite write lock across the outbound call
    name, msg, status = "", "That is too long.", 400
    if len(secret) <= MAX_LEN and len(username) <= MAX_LEN:
        if service == "listenbrainz":
            msg = "ListenBrainz did not accept that token."
            try:
                name = (await validator.listenbrainz_username(secret) if secret else None) or ""
            except TransientError:
                name, msg = "", "Couldn't reach ListenBrainz; try again."
        elif service == "discogs":
            msg = "Discogs did not accept that token."
            try:
                # a header value: control and non-ASCII characters are refused before any request
                ok = secret.isascii() and secret.isprintable()
                name = (await validator.discogs_username(secret) if secret and ok else None) or ""
            except TransientError:
                name, msg = "", "Couldn't reach Discogs; try again."
        else:
            name, msg = "", "Navidrome did not accept that username and password."
            keys = (("navidrome-user", str(user.id)), ("navidrome-target", username.lower()))
            # shortcut: shares the login limiter's in-process state and 10 per 15 minutes policy;
            # a rejected guess counts against the user and the target; a link clears the target.
            if username and secret and not admit(*keys):
                msg, status = "Too many attempts, try again later.", 429
            elif username and secret:
                match await validator.navidrome_ping(username, secret):
                    case Ping.ok:
                        name = username
                        refund(keys[0])  # a success is not a failed guess
                        forget(keys[1])
                    case Ping.unreachable:
                        msg = "Couldn't reach Navidrome; try again."
                        refund(*keys)
                    case Ping.rejected:
                        pass
    for attempt in (1, 2):  # a double submit can lose the insert race; retry as an update
        try:
            with request.app.state.session_factory.begin() as s:  # db is spent after the commit
                if not name:
                    return _page(request, user, s, box, (service, msg), status)
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
def unlink(request: Request, service: str, user: CurrentUser, db: Db) -> RedirectResponse:
    link = get_link(db, user.id, _service(request, service, linking=False))
    if link is not None:
        db.delete(link)
    return RedirectResponse("/account", 303)
