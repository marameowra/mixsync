"""Admin settings page (ADR 0021): view, save and reset app settings; changes apply at restart.

Form semantics: a non-blank field is saved (even if equal to the environment), a blank one is
left as it is, and Reset is the only way to clear a saved value. Saved values are prefilled; the
effective value of an unsaved one is shown as a placeholder, so Save never saves it by accident.
The auto-accept cap is the exception: it is its own value, and blank clears it."""

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.orm import Session

from mixsync.acquire.requests import auto_accept_cap
from mixsync.core.capabilities import Capability
from mixsync.core.config import Settings
from mixsync.db.app_settings import EDITABLE, resolve
from mixsync.db.auth import AuthUser
from mixsync.db.models.auth import AppSettings, MatchingProfile
from mixsync.match.profiles import confidence, from_confidence
from mixsync.web.deps import Db, require
from mixsync.web.routes.account import percent
from mixsync.web.templating import templates

router = APIRouter(prefix="/settings")
Admin = Annotated[AuthUser, Depends(require(Capability.admin))]

GROUPS: dict[str, tuple[str, ...]] = {
    "MusicBrainz and metadata": (
        "mb_contact",
        "mb_rate",
        "mb_base_url",
        "deezer_suggestions",
        "discogs_suggestions",
    ),
    "slskd": ("slskd_url", "slskd_max_searches", "slskd_search_debounce"),
    "Downloads": (
        "max_concurrent_downloads",
        "slow_download_window",
        "slow_download_floor",
        "slow_download_share",
        "adopt_downloads",
        "adopt_settle_minutes",
    ),
    "Navidrome": (
        "navidrome_url",
        "navidrome_user",
        "navidrome_library",
        "navidrome_discover_library",
    ),
    "Library and organize": (
        "flac_level",
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
)

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
) -> HTMLResponse:
    settings, resolved = resolve(db)
    cap = auto_accept_cap(db)
    errors, submitted = errors or {}, submitted or {}
    fields: dict[str, dict[str, Any]] = {}
    for name in EDITABLE:
        r = resolved[name]
        mine = r.source == "saved"
        fields[name] = {
            "name": name,
            "env": f"MIXSYNC_{name.upper()}",
            "kind": "bool" if isinstance(r.value, bool) else "text",
            "source": r.source,
            "effective": _text(r.value),
            "value": submitted[name] if name in submitted else _text(r.value) if mine else "",
            "warn": mine and r.env_set,
            "error": errors.get(name),
        }
    ctx = {
        "user": user,
        "groups": {title: [fields[n] for n in names] for title, names in GROUPS.items()},
        "read_only": {n: _text(getattr(settings, n)) or "not set" for n in READ_ONLY},
        "cap": submitted["cap"] if "cap" in submitted else "" if cap is None else confidence(cap),
        "cap_error": errors.get("cap"),
        "saved": saved,
        "has_errors": bool(errors),
    }
    return templates.TemplateResponse(
        request, "pages/settings.html", ctx, status_code=400 if errors else 200
    )


@router.get("")
def show(request: Request, admin: Admin, db: Db, saved: int = 0) -> HTMLResponse:
    return _page(request, admin, db, saved=bool(saved))


@router.post("")
async def save(request: Request, admin: Admin, db: Db) -> Response:
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
    row = db.get(AppSettings, 1) or AppSettings(id=1)
    for name, value in values.items():
        setattr(row, name, value)
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


@router.post("/reset/{name}")
def reset(name: str, admin: Admin, db: Db) -> Response:
    if name not in EDITABLE:
        raise HTTPException(404)
    row = db.get(AppSettings, 1)
    if row:
        setattr(row, name, None)
    return RedirectResponse("/settings?saved=1", 303)
