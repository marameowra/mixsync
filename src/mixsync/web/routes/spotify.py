"""Spotify playlist import (#337): connect (OAuth with PKCE), list the user's own playlists,
import one as a mirrored playlist, disconnect. Spotify is called only from these user actions.
Imported playlists never become requests (discovery/spotify_api.py)."""

import hmac
import json
import secrets
import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError

from mixsync.core.capabilities import Capability
from mixsync.core.errors import PermanentError, TransientError
from mixsync.db.auth import AuthUser
from mixsync.db.models.accounts import UserLink
from mixsync.db.secretbox import link_secret, set_secret
from mixsync.discovery import spotify_api
from mixsync.discovery.spotify_api import SERVICE, Revoked, SpotifyClient
from mixsync.web.csrf import csrf_token
from mixsync.web.deps import Box, CurrentUser, Db, require
from mixsync.web.routes.account import get_link
from mixsync.web.templating import templates

router = APIRouter(prefix="/spotify")
User = Annotated[AuthUser, Depends(require(Capability.request))]
OAUTH_COOKIE = "mixsync_spotify_oauth"
OAUTH_TTL = 600  # seconds the user has to approve on Spotify


def _client(request: Request) -> SpotifyClient:
    services = request.app.state.services
    if not services.settings.spotify_client_id:
        raise HTTPException(404, "Spotify import is off: no Spotify client ID is set")
    return SpotifyClient(services.spotify_http, services.settings.spotify_client_id)


def redirect_uri(request: Request) -> str:
    return str(request.url_for("spotify_callback"))


@router.post("/connect")
def connect(request: Request, user: CurrentUser, box: Box) -> RedirectResponse:
    """Off to Spotify. The verifier and state ride in an encrypted cookie bound to this login
    session (its CSRF token), checked on the way back."""
    client = _client(request)
    verifier, challenge = spotify_api.pkce()
    state = secrets.token_urlsafe(24)
    url = spotify_api.authorize_url(client.client_id, redirect_uri(request), state, challenge)
    sealed = box.encrypt(
        json.dumps(
            {"state": state, "verifier": verifier, "csrf": csrf_token(request), "at": time.time()}
        )
    )
    resp = RedirectResponse(url, 303)
    resp.set_cookie(
        OAUTH_COOKIE,
        sealed,
        max_age=OAUTH_TTL,
        path="/spotify/callback",
        httponly=True,
        samesite="lax",  # sent on Spotify's top-level redirect back
        secure=request.url.scheme == "https",
    )
    return resp


def _check_state(request: Request, box: Box, state: str) -> str:
    """The verifier, if the callback belongs to this session's own fresh connect."""
    plain = box.decrypt(request.cookies.get(OAUTH_COOKIE, ""))
    try:
        sealed = json.loads(plain or "")
        fresh = time.time() - float(sealed["at"]) < OAUTH_TTL
        same = hmac.compare_digest(str(sealed["csrf"]), csrf_token(request))
        if fresh and same and hmac.compare_digest(str(sealed["state"]), state):
            return str(sealed["verifier"])
    except (ValueError, KeyError, TypeError):
        pass
    raise HTTPException(400, "This Spotify login does not match your session; connect again.")


@router.get("/callback", name="spotify_callback")
async def callback(
    request: Request,
    user: CurrentUser,
    db: Db,
    box: Box,
    state: str = "",
    code: str = "",
    error: str = "",
) -> RedirectResponse:
    client = _client(request)
    verifier = _check_state(request, box, state)
    if error or not code:
        raise HTTPException(400, "Spotify did not grant access; nothing was linked.")
    db.commit()  # don't hold the SQLite write lock across the outbound calls
    try:
        access, refresh = await client.exchange(code, redirect_uri(request), verifier)
        me = await client.me(access)
    except TransientError:
        raise HTTPException(503, "Couldn't reach Spotify; try again.") from None
    except PermanentError as e:
        raise HTTPException(400, f"Spotify login failed: {e}") from None
    with request.app.state.session_factory.begin() as s:  # db is spent after the commit
        link = get_link(s, user.id, SERVICE)
        if link is None:
            link = UserLink(user_id=user.id, service=SERVICE)
            s.add(link)
        link.external_username = me[:255]
        set_secret(box, link, refresh)
        link.verified_at = datetime.now(UTC)
    resp = RedirectResponse("/account", 303)
    resp.delete_cookie(OAUTH_COOKIE, path="/spotify/callback")
    return resp


@router.post("/disconnect")
def disconnect(user: CurrentUser, db: Db) -> RedirectResponse:
    spotify_api.disconnect(db, user.id)
    return RedirectResponse("/account", 303)


async def _session(request: Request, db: Db, box: Box, user: AuthUser) -> tuple[str, str]:
    """A fresh access token and the Spotify user id. A refused refresh token marks the link
    for reconnecting."""
    client = _client(request)
    link = get_link(db, user.id, SERVICE)
    refresh = link_secret(box, link) if link else None  # "" (refused) never decrypts
    if link is None or refresh is None:
        raise HTTPException(400, "Connect Spotify on your account page first.")
    me = link.external_username
    db.commit()  # don't hold the SQLite write lock across the outbound calls
    try:
        access, new = await client.refresh(refresh)
    except Revoked:
        new, access = "", ""
    except TransientError:
        raise HTTPException(503, "Couldn't reach Spotify; try again.") from None
    except PermanentError as e:
        raise HTTPException(502, str(e)) from None
    if new != refresh:
        with request.app.state.session_factory.begin() as s:
            if link := get_link(s, user.id, SERVICE):
                link.secret_encrypted = box.encrypt(new) if new else ""  # "" never decrypts
                link.needs_relink = not new
    if not access:
        raise HTTPException(400, "Spotify refused the stored login; reconnect Spotify.")
    return access, me


@router.get("/playlists")
async def playlists(request: Request, user: User, db: Db, box: Box) -> HTMLResponse:
    """The user's own and collaborative Spotify playlists, each with an Import button."""
    access, me = await _session(request, db, box, user)
    try:
        rows = await _client(request).playlists(access, me)
    except TransientError:
        raise HTTPException(503, "Couldn't reach Spotify; try again.") from None
    except PermanentError as e:
        raise HTTPException(502, str(e)) from None
    ctx = {"user": user, "rows": rows}
    return templates.TemplateResponse(request, "pages/spotify_playlists.html", ctx)


@router.post("/import")
async def import_playlist(
    request: Request,
    user: User,
    db: Db,
    box: Box,
    playlist_id: Annotated[str, Form()],
    name: Annotated[str, Form()],
) -> RedirectResponse:
    """Read the playlist from Spotify once and (re)build its mirror; nothing is requested."""
    name = name.strip()
    if not name:
        raise HTTPException(400, "the playlist has no name")
    access, _ = await _session(request, db, box, user)
    try:
        tracks = await _client(request).tracks(access, playlist_id)
    except TransientError:
        raise HTTPException(503, "Couldn't reach Spotify; try again.") from None
    except PermanentError as e:
        raise HTTPException(400, f"Couldn't read that playlist: {e}") from None
    if not tracks:
        raise HTTPException(400, f"playlist {name!r} has no tracks")
    try:
        with request.app.state.session_factory.begin() as s:
            spotify_api.save(s, datetime.now(UTC), user.id, playlist_id, name, tracks)
    except IntegrityError:
        raise HTTPException(409, f"you already have a playlist named {name!r}") from None
    return RedirectResponse("/mirrored", 303)
