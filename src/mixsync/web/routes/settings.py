"""Admin settings page (ADR 0021): view, save and reset app settings; changes apply at restart.

Form semantics: a non-blank field is saved (even if equal to the environment), a blank one is
left as it is, and Reset is the only way to clear a saved value. Secrets are write-only: never
rendered back, blank keeps the saved one, Clear removes it. Saved values are prefilled; the
effective value of an unsaved one is shown as a placeholder, so Save never saves it by accident.
The auto-accept cap is the exception: it is its own value, and blank clears it."""

from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import inspect, update
from sqlalchemy.orm import Session

from mixsync.acquire import missing_scan
from mixsync.acquire.requests import auto_accept_cap
from mixsync.core.capabilities import Capability
from mixsync.core.config import Settings
from mixsync.core.jobs import LIVE
from mixsync.db.app_settings import (
    EDITABLE,
    SECRETS,
    resolve,
    restart_pending,
    server_link_users,
    switch_media_server,
)
from mixsync.db.auth import AuthUser
from mixsync.db.models.auth import AppSettings, MatchingProfile
from mixsync.db.models.library import Plan
from mixsync.match.profiles import confidence, from_confidence
from mixsync.web.deps import Box, Db, require
from mixsync.web.routes.account import percent
from mixsync.web.templating import render, templates

router = APIRouter(prefix="/settings")
Admin = Annotated[AuthUser, Depends(require(Capability.admin))]

GROUPS: dict[str, tuple[str, ...]] = {
    "MusicBrainz and metadata": (
        "mb_contact",
        "mb_rate",
        "mb_base_url",
        "deezer_suggestions",
        "discogs_suggestions",
        "spotify_client_id",
    ),
    "slskd": ("slskd_url", "slskd_max_searches", "slskd_search_debounce"),
    "Torrent and Usenet": (
        "prowlarr_url",
        "qbittorrent_url",
        "qbittorrent_username",
        "sabnzbd_url",
    ),
    "Downloads": (
        "max_concurrent_downloads",
        "slow_download_window",
        "slow_download_floor",
        "slow_download_share",
        "adopt_downloads",
        "adopt_settle_minutes",
    ),
    "Media server": (
        "media_server_kind",
        "media_server_url",
        "media_server_user",
        "media_server_library",
        "media_server_discover_library",
    ),
    "Library and organize": (
        "flac_level",
        "embed_cover",
        "canonical_media",
        "canonical_countries",
        "trash_days",
        "timezone",
        "path_template",
    ),
    "Logging": ("log_level",),
}
# bootstrap and mount settings shown read-only; secrets and the database URL (it may carry a
# password) are never shown
READ_ONLY = (
    "config_dir",
    "data_dir",
    "downloads_dir",
    "drop_dir",
    "import_source_dir",
    "backup_dir",
    "qbittorrent_dir",
    "sabnzbd_dir",
)


def _help(name: str) -> str:
    return Settings.model_fields[name].description or ""


CAP_ERROR = "Enter a whole number from 0 to 100, or leave it blank."


def _text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in cast("list[Any]", value))
    return "" if value is None else str(value)


def _parse(name: str, raw: str) -> Any:
    """The typed value for a submitted field, validated by Settings itself."""
    default = Settings.model_fields[name].default
    value: Any = (
        [p.strip() for p in raw.split(",") if p.strip()] if isinstance(default, list) else raw
    )
    return getattr(Settings(**{name: value}), name) if value != [] else None  # ",": blank


def _page(
    request: Request,
    user: AuthUser,
    db: Session,
    errors: dict[str, str] | None = None,
    submitted: dict[str, str] | None = None,
    saved: bool = False,
    kind_change: str | None = None,
) -> HTMLResponse:
    settings, resolved = resolve(db)
    cap = auto_accept_cap(db)
    services = request.app.state.services
    errors, submitted = errors or {}, submitted or {}
    fields: dict[str, dict[str, Any]] = {}
    for name in EDITABLE:
        r = resolved[name]
        mine = r.source == "saved"
        fields[name] = {
            "name": name,
            "help": _help(name),
            "env": f"MIXSYNC_{name.upper()}",
            "kind": "bool" if isinstance(r.value, bool) else "text",
            "source": r.source,
            "effective": _text(r.value),
            "value": submitted[name] if name in submitted else _text(r.value) if mine else "",
            "warn": mine and r.env_set,
            "error": errors.get(name),
        }
    secrets = [
        {
            "name": n,
            "help": _help(n),
            "env": f"MIXSYNC_{n.upper()}",
            "source": resolved[n].source,
            "unreadable": resolved[n].unreadable,
            "warn": resolved[n].source == "saved" and resolved[n].env_set,
        }
        for n in SECRETS
    ]
    ctx = {
        "user": user,
        "secrets": secrets,
        "groups": {title: [fields[n] for n in names] for title, names in GROUPS.items()},
        "read_only": {n: (_text(getattr(settings, n)) or "not set", _help(n)) for n in READ_ONLY},
        "cap": submitted["cap"] if "cap" in submitted else "" if cap is None else confidence(cap),
        "cap_error": errors.get("cap"),
        "saved": saved,
        "has_errors": bool(errors),
        "restart": restart_pending(db, services.settings_version, services.clock.now()),
        "kind_change": kind_change
        and {
            "from": settings.media_server_kind,
            "to": kind_change,
            "users": server_link_users(db),
        },
    }
    return templates.TemplateResponse(
        request, "pages/settings.html", ctx, status_code=400 if errors else 200
    )


def _stamp(row: AppSettings) -> None:
    """Bump the settings version only when an app setting changed: the cap is read live."""
    attrs = inspect(row).attrs
    names = (*EDITABLE, *(f"{n}_encrypted" for n in SECRETS))
    if any(attrs[n].history.has_changes() for n in names):
        row.settings_saved_at = datetime.now(UTC)


@router.get("")
def show(request: Request, admin: Admin, db: Db, saved: int = 0) -> HTMLResponse:
    return _page(request, admin, db, saved=bool(saved))


@router.post("")
async def save(request: Request, admin: Admin, db: Db, box: Box) -> Response:
    form = await request.form()
    submitted = {n: str(form.get(n, "")).strip() for n in (*EDITABLE, "cap")}
    errors: dict[str, str] = {}
    values: dict[str, Any] = {}
    for name, raw in submitted.items():
        if name == "cap" or not raw:
            continue
        try:
            if (value := _parse(name, raw)) is not None:
                values[name] = value
        except ValidationError as e:
            errors[name] = str(e.errors()[0]["msg"]).removeprefix("Value error, ")
    cap_text = submitted["cap"]
    pct = percent(cap_text) if cap_text else None
    if cap_text and pct is None:
        errors["cap"] = CAP_ERROR
    if errors:
        return _page(request, admin, db, errors, submitted)
    kind = values.get("media_server_kind")
    if kind is not None and kind != resolve(db)[0].media_server_kind:
        if form.get("confirm_kind") != kind:  # nothing is saved until the admin confirms
            return _page(request, admin, db, submitted=submitted, kind_change=kind)
        switch_media_server(db, kind)
    row = db.get(AppSettings, 1) or AppSettings(id=1)
    for name, value in values.items():
        setattr(row, name, value)
    for name in SECRETS:
        raw = str(form.get(name, ""))
        if raw.strip():  # blank decided on the stripped value; the secret is kept exactly
            setattr(row, f"{name}_encrypted", box.encrypt(raw))
    _stamp(row)
    row.min_auto_accept_confidence = pct
    db.add(row)
    if pct is not None:
        cap = from_confidence(pct)
        db.execute(
            update(MatchingProfile)
            .where(MatchingProfile.auto_accept_max > cap)
            .values(auto_accept_max=cap)
        )
    return RedirectResponse("/settings?saved=1", 303)


@router.post("/missing-tracks/scan")
def missing_tracks_scan(admin: Admin, db: Db) -> Response:
    missing_scan.enqueue_scan(db, datetime.now(UTC))
    return RedirectResponse("/settings/missing-tracks", 303)


@router.get("/missing-tracks")
def missing_tracks(request: Request, admin: Admin, db: Db) -> HTMLResponse:
    """A live region (ADR 0017): scanning, then the preview, or why the scan failed."""
    job, plan = missing_scan.latest(db)
    ctx = {"user": admin, "job": job, "plan": plan, "scanning": bool(job and job.status in LIVE)}
    return render(request, "pages/missing_tracks.html", "pages/missing_tracks_live.html", ctx)


def _scan_draft(db: Session, plan_id: int) -> Plan:
    plan = db.get(Plan, plan_id, with_for_update=True)  # a double confirm reopens once
    if plan is None or plan.kind != missing_scan.KIND:
        raise HTTPException(404, "no such scan")
    if plan.status != "draft":
        raise HTTPException(409, f"scan is {plan.status}")
    return plan


@router.post("/missing-tracks/{plan_id}/confirm")
def missing_tracks_confirm(plan_id: int, admin: Admin, db: Db) -> Response:
    missing_scan.confirm(db, datetime.now(UTC), _scan_draft(db, plan_id))
    return RedirectResponse("/settings/missing-tracks", 303)


@router.post("/missing-tracks/{plan_id}/discard")
def missing_tracks_discard(plan_id: int, admin: Admin, db: Db) -> Response:
    _scan_draft(db, plan_id).status = "discarded"
    return RedirectResponse("/settings/missing-tracks", 303)


@router.post("/reset/{name}")
def reset(name: str, admin: Admin, db: Db) -> Response:
    if name not in EDITABLE:
        raise HTTPException(404)
    if (
        name == "media_server_kind"
        and Settings().media_server_kind != resolve(db)[0].media_server_kind
    ):
        raise HTTPException(409, "Reset would switch the media server; save the kind instead.")
    row = db.get(AppSettings, 1)
    if row:
        setattr(row, name, None)
        _stamp(row)
    return RedirectResponse("/settings?saved=1", 303)


@router.post("/clear/{name}")
def clear(name: str, admin: Admin, db: Db) -> Response:
    if name not in SECRETS:
        raise HTTPException(404)
    row = db.get(AppSettings, 1)
    if row:
        setattr(row, f"{name}_encrypted", None)
        _stamp(row)
    return RedirectResponse("/settings?saved=1", 303)
