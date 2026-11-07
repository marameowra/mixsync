from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mixsync.acquire.requests import auto_accept_cap, profile_for
from mixsync.core.capabilities import Capability
from mixsync.core.errors import PermanentError, TransientError
from mixsync.core.jobs import WORKER_STALE_AFTER
from mixsync.core.matching import Profile
from mixsync.db.auth import AuthUser, set_password, verify_password
from mixsync.db.models.accounts import UserLink
from mixsync.db.models.auth import AppSettings, MatchingProfile, User
from mixsync.db.queue import worker_age
from mixsync.db.secretbox import SecretBox, set_secret, user_links
from mixsync.discovery import playlists
from mixsync.discovery.listenbrainz import ListenBrainzClient
from mixsync.match.candidates import QUALITY_TIERS
from mixsync.match.profiles import BALANCED, LOOSE, STRICT, confidence, from_confidence
from mixsync.metadata.discogs import DiscogsClient
from mixsync.ratelimit.client import PoliteClient
from mixsync.targets.subsonic import SubsonicClient
from mixsync.web.deps import (
    SESSION_COOKIE,
    Box,
    CurrentUser,
    Db,
    get_discogs,
    get_listenbrainz_http,
    get_navidrome_http,
    require,
)
from mixsync.web.routes.auth import admit, forget, refund
from mixsync.web.templating import templates

router = APIRouter(prefix="/account")
SERVICES = ("listenbrainz", "navidrome", "discogs")
PRESETS = (STRICT, BALANCED, LOOSE)
Admin = Annotated[AuthUser, Depends(require(Capability.admin))]


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
    profile, cap = profile_for(s, user.id), auto_accept_cap(s)
    me = s.get_one(User, user.id)
    mine = s.get(MatchingProfile, me.matching_profile_id) if me.matching_profile_id else None
    ctx = {
        "presets": [
            (p.name, confidence(p.auto_accept_max), confidence(p.review_max)) for p in PRESETS
        ],
        "preset": mine.preset if mine else BALANCED.name,
        "capped": [p.name for p in PRESETS if cap is not None and p.auto_accept_max > cap],
        "cap": None if cap is None else confidence(cap),
        "user": user,
        "links": by_service,
        "error": error,
        "discogs": discogs,
        "auto_accept": confidence(profile.auto_accept_max),
        "review": confidence(profile.review_max),
        "tiers": [
            (name, label, quality.tiers.index(name) + 1 if name in quality.tiers else None)
            for name, (label, _) in QUALITY_TIERS.items()
        ]
        if (quality := profile.quality)
        else [(name, label, None) for name, (label, _) in QUALITY_TIERS.items()],
        "floor": profile.quality.floor_kbps if profile.quality else None,
    }
    return templates.TemplateResponse(
        request, "pages/account.html", ctx, status_code=status if error else 200
    )


@router.get("")
def show(request: Request, user: CurrentUser, db: Db, box: Box) -> HTMLResponse:
    return _page(request, user, db, box)


def _percent(text: str) -> int | None:
    try:
        pct = int(text.strip())
    except ValueError:
        return None
    return pct if 0 <= pct <= 100 else None


def _own_row(db: Session, user_id: int, current: Profile) -> MatchingProfile:
    """The user's own matching row; a missing or shared one is replaced by a copy first."""
    row = db.get_one(User, user_id)
    mine = db.get(MatchingProfile, row.matching_profile_id) if row.matching_profile_id else None
    users = select(func.count(User.id)).where(User.matching_profile_id == row.matching_profile_id)
    if mine is None or db.scalar(users) != 1:  # never change a row another user points at
        mine = MatchingProfile(
            name="custom",
            preset="custom",
            auto_accept_max=current.auto_accept_max,
            review_max=current.review_max,
            quality_pref=mine.quality_pref if mine else None,
        )
        db.add(mine)
        db.flush()
        row.matching_profile_id = mine.id
    return mine


@router.post("/matching/preset")
def save_preset(
    request: Request,
    user: CurrentUser,
    db: Db,
    box: Box,
    preset: Annotated[str, Form()],
) -> Response:
    """Replaces the user's limits with a preset's. Stored decisions are not re-banded."""
    chosen = next((p for p in PRESETS if p.name == preset), None)
    if chosen is None:
        return _page(request, user, db, box, ("matching", "Choose Strict, Balanced or Loose."))
    mine = _own_row(db, user.id, profile_for(db, user.id))
    mine.name = mine.preset = chosen.name
    mine.auto_accept_max, mine.review_max = chosen.auto_accept_max, chosen.review_max
    return RedirectResponse("/account", 303)


@router.post("/matching")
def save_matching(
    request: Request,
    user: CurrentUser,
    db: Db,
    box: Box,
    auto_accept: Annotated[str, Form()],
) -> Response:
    """The user's auto-accept limit, entered as confidence. review_max is carried over."""
    current, cap = profile_for(db, user.id), auto_accept_cap(db)
    pct = _percent(auto_accept)
    limit = from_confidence(pct) if pct is not None else None
    if limit is None or limit > current.review_max:
        msg = f"Enter a whole number from {confidence(current.review_max)} to 100."
        return _page(request, user, db, box, ("matching", msg))
    if cap is not None and limit > cap:
        msg = f"The administrator's minimum is {confidence(cap)}%."
        return _page(request, user, db, box, ("matching", msg))
    mine = _own_row(db, user.id, current)
    mine.name, mine.preset, mine.auto_accept_max = "custom", "custom", limit
    return RedirectResponse("/account", 303)


@router.post("/matching/quality")
async def save_quality(request: Request, user: CurrentUser, db: Db, box: Box) -> Response:
    """The user's quality order (`rank_<tier>`: 1 is best, blank leaves the tier out) and lossy
    floor in kbps. The preset name is kept: quality does not touch the limits."""
    form = await request.form()
    ranks: dict[str, int] = {}
    for name in QUALITY_TIERS:
        text = str(form.get(f"rank_{name}", "")).strip()
        if text:
            if not (text.isascii() and text.isdigit()) or not 1 <= int(text) <= len(QUALITY_TIERS):
                msg = f"Number each quality you accept from 1 to {len(QUALITY_TIERS)}."
                return _page(request, user, db, box, ("matching", msg))
            ranks[name] = int(text)
    floor_text = str(form.get("floor", "")).strip()
    if floor_text and (
        not (floor_text.isascii() and floor_text.isdigit()) or not 1 <= int(floor_text) <= 2000
    ):
        msg = "Enter the minimum bitrate as a whole number of kbps, or leave it blank."
        return _page(request, user, db, box, ("matching", msg))
    floor = int(floor_text) if floor_text else None
    # ties keep the listed order (dicts and sorted are both stable)
    tiers = sorted(ranks, key=ranks.__getitem__)
    me = db.get_one(User, user.id)
    row = db.get(MatchingProfile, me.matching_profile_id) if me.matching_profile_id else None
    name, preset = (row.name, row.preset) if row else (BALANCED.name, BALANCED.name)
    # raw limits, not profile_for's: the admin cap is applied on load, never stored
    raw = row or BALANCED
    mine = _own_row(db, user.id, Profile(name, raw.auto_accept_max, raw.review_max))
    mine.name, mine.preset = name, preset
    mine.quality_pref = {"tiers": tiers, "floor_kbps": floor} if tiers or floor else None
    return RedirectResponse("/account", 303)


def _cap_page(request: Request, user: AuthUser, db: Db, error: str | None = None) -> HTMLResponse:
    cap = auto_accept_cap(db)
    ctx = {"user": user, "cap": None if cap is None else confidence(cap), "error": error}
    return templates.TemplateResponse(
        request, "pages/matching_cap.html", ctx, status_code=400 if error else 200
    )


@router.get("/cap")
def show_cap(request: Request, admin: Admin, db: Db) -> HTMLResponse:
    return _cap_page(request, admin, db)


@router.post("/cap")
def save_cap(
    request: Request, admin: Admin, db: Db, minimum: Annotated[str, Form()] = ""
) -> Response:
    """Blank clears the cap; already clamped profiles keep their values."""
    pct = _percent(minimum) if minimum.strip() else None
    if minimum.strip() and pct is None:
        return _cap_page(
            request, admin, db, "Enter a whole number from 0 to 100, or leave it blank."
        )
    settings = db.get(AppSettings, 1) or AppSettings(id=1)
    settings.min_auto_accept_confidence = pct
    db.add(settings)
    if pct is not None:
        cap = from_confidence(pct)
        db.execute(
            update(MatchingProfile)
            .where(MatchingProfile.auto_accept_max > cap)
            .values(auto_accept_max=cap)
        )
    return RedirectResponse("/account/cap", 303)


@router.get("/worker")
def show_worker(request: Request, admin: Admin, db: Db) -> HTMLResponse:
    age = worker_age(db, request.app.state.services.clock.now())
    ctx = {
        "user": admin,
        "seconds": None if age is None else max(0, round(age.total_seconds())),
        "stale": age is None or age > WORKER_STALE_AFTER,
    }
    return templates.TemplateResponse(request, "pages/worker.html", ctx)


@router.post("/password")
def change_password(
    request: Request,
    user: CurrentUser,
    db: Db,
    box: Box,
    current: Annotated[str, Form()],
    new: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
) -> Response:
    """The current session stays signed in; the user's other sessions end."""
    row = db.get_one(User, user.id)
    key = (request.client.host if request.client else "", user.username.lower())
    if not admit(key):  # the same budget as /login
        msg = "Too many attempts, try again later."
        return _page(request, user, db, box, ("password", msg), 429)
    if not verify_password(row.password_hash, current):
        return _page(request, user, db, box, ("password", "The current password is wrong."))
    forget(key)
    if not new or len(new) > MAX_LEN:
        return _page(request, user, db, box, ("password", "Enter a new password."))
    if new != confirm:
        return _page(request, user, db, box, ("password", "The new passwords do not match."))
    set_password(db, row, new, keep=request.cookies.get(SESSION_COOKIE))
    return RedirectResponse("/account", 303)


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
