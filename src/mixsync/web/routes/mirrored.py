"""Mirrored playlists: list, cadence, sync now, confirm the missing-track requests, delete.
Mirrors are made by upstream adapters (`discovery.mirrored.create`); the Loves one and the
Spotify-file upload from here."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from mixsync.core.capabilities import Capability
from mixsync.core.errors import PermanentError
from mixsync.db.auth import AuthUser
from mixsync.db.models.accounts import UserLink
from mixsync.db.models.discovery import MirroredPlaylist
from mixsync.discovery import loves, mirrored, spotify_file
from mixsync.web.deps import Db, require
from mixsync.web.templating import templates

router = APIRouter(prefix="/mirrored")
User = Annotated[AuthUser, Depends(require(Capability.request))]


def _own(db: Db, user: AuthUser, playlist_id: int) -> MirroredPlaylist:
    pl = db.get(MirroredPlaylist, playlist_id)
    if pl is None:
        raise HTTPException(404, "no such playlist")
    if pl.user_id != user.id:
        raise HTTPException(403, "not your playlist")
    return pl


@router.get("")
def playlist_list(request: Request, user: User, db: Db) -> HTMLResponse:
    rows = db.scalars(
        select(MirroredPlaylist)
        .where(MirroredPlaylist.user_id == user.id)
        .order_by(MirroredPlaylist.id)
    ).all()
    tracks = {p.id: mirrored.track_states(db, p) for p in rows}
    missing = {
        p.id: [e for e, state in tracks[p.id] if state == mirrored.MISSING]
        for p in rows
        if p.upstream_kind not in mirrored.NEVER_REQUEST  # listed in the table, never offered
    }
    ctx = {
        "user": user,
        "rows": rows,
        "tracks": tracks,
        "missing": missing,
        "lb_linked": _lb_linked(db, user),
        "has_loves": any(p.upstream_kind == mirrored.LOVES for p in rows),
        "spotify_on": bool(request.app.state.services.settings.spotify_client_id),
        "spotify_linked": _linked(db, user, "spotify"),
    }
    return templates.TemplateResponse(request, "pages/mirrored.html", ctx)


def _lb_linked(db: Db, user: AuthUser) -> bool:
    return _linked(db, user, "listenbrainz")


def _linked(db: Db, user: AuthUser, service: str) -> bool:
    q = select(UserLink.id).where(UserLink.user_id == user.id, UserLink.service == service)
    return db.scalar(q) is not None


@router.post("/loves")
def create_loves(user: User, db: Db) -> RedirectResponse:
    """The user's Loves mirror of their ListenBrainz loved tracks; a second click is a no-op."""
    if not _lb_linked(db, user):
        raise HTTPException(400, "link ListenBrainz on /account first")
    try:
        with db.begin_nested():  # a racing double submit hits the unique upstream key
            loves.create_loves(db, datetime.now(UTC), user.id)
    except IntegrityError:
        taken = db.scalar(
            select(MirroredPlaylist.id).where(
                MirroredPlaylist.user_id == user.id,
                MirroredPlaylist.upstream_kind == mirrored.LOVES,
            )
        )
        if taken is None:
            raise HTTPException(409, f"you already have a playlist named {loves.NAME!r}") from None
    return RedirectResponse("/mirrored", status_code=303)


@router.post("/spotify")
async def upload_spotify(
    request: Request,
    user: User,
    db: Db,
    file: Annotated[UploadFile, File()],
    name: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Create or update a mirror from an uploaded Spotify playlist file. The whole file is
    checked before anything is written; a data export with several playlists needs `name`."""
    try:
        data = await file.read(spotify_file.MAX_BYTES + 1)
        found = spotify_file.parse(data, file.filename or "", name)  # name also names a CSV
        pick = name.strip()[:255] or (next(iter(found)) if len(found) == 1 else "")
        if pick not in found:
            raise spotify_file.BadFile(
                f"playlist {pick!r} is not in the file"
                if pick
                else f"the file has several playlists; enter the name of one of: {', '.join(found)}"
            )
        if not found[pick]:
            raise spotify_file.BadFile(f"playlist {pick!r} has no tracks")
    except spotify_file.BadFile as e:
        raise HTTPException(400, str(e)) from None
    root = Path(request.app.state.settings.config_dir) / "spotify"
    try:
        with db.begin_nested():
            spotify_file.SpotifyFile(root).upload(db, datetime.now(UTC), user.id, pick, found[pick])
    except IntegrityError:
        raise HTTPException(409, f"you already have a playlist named {pick!r}") from None
    return RedirectResponse("/mirrored", status_code=303)


@router.post("/{playlist_id}/settings")
def settings(
    playlist_id: int,
    user: User,
    db: Db,
    cadence_days: Annotated[str, Form()] = "",  # empty reaches our 400, not FastAPI's 422
) -> RedirectResponse:
    pl = _own(db, user, playlist_id)
    v = cadence_days.strip()
    if not (v.isascii() and v.isdigit() and 1 <= int(v) <= 365):
        raise HTTPException(400, "cadence is a whole number of days from 1 to 365")
    pl.cadence_days = int(v)
    return RedirectResponse("/mirrored", status_code=303)


@router.post("/{playlist_id}/sync")
def sync_now(playlist_id: int, user: User, db: Db) -> RedirectResponse:
    pl = _own(db, user, playlist_id)
    if not mirrored.sync_now(db, datetime.now(UTC), pl):
        raise HTTPException(409, "synced within the last hour")
    return RedirectResponse("/mirrored", status_code=303)


@router.post("/{playlist_id}/request")
async def request_missing(
    playlist_id: int, request: Request, user: User, db: Db
) -> RedirectResponse:
    """The confirmation: requests only the missing tracks the page listed (by release)."""
    pl = _own(db, user, playlist_id)
    shown = {str(r) for r in (await request.form()).getlist("release")}
    try:
        mirrored.request_missing(db, datetime.now(UTC), pl, shown)
    except PermanentError as e:  # a Spotify import: missing tracks are only listed
        raise HTTPException(403, str(e)) from None
    return RedirectResponse("/mirrored", status_code=303)


@router.get("/{playlist_id}/delete")
def delete_confirm(playlist_id: int, request: Request, user: User, db: Db) -> HTMLResponse:
    pl = _own(db, user, playlist_id)
    return templates.TemplateResponse(
        request, "pages/mirrored_delete.html", {"user": user, "pl": pl}
    )


@router.post("/{playlist_id}/delete")
def delete(playlist_id: int, user: User, db: Db) -> RedirectResponse:
    db.delete(_own(db, user, playlist_id))  # its queued sync finds no row and stops
    return RedirectResponse("/mirrored", status_code=303)
