import re
import unicodedata
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from mixsync.acquire import review
from mixsync.acquire.evidence import Evidence
from mixsync.core.capabilities import Capability
from mixsync.core.errors import PermanentError, TransientError
from mixsync.db import decisions
from mixsync.db.auth import AuthUser
from mixsync.db.models.matching import MatchDecision
from mixsync.db.secretbox import link_secret, user_links
from mixsync.library import tags
from mixsync.match.candidates import parse_name
from mixsync.metadata.deezer import DeezerAlbum
from mixsync.metadata.discogs import DiscogsRelease
from mixsync.web.deps import Db, Deezer, Discogs, Namer, decision_for, require
from mixsync.web.names import MAX_LOOKUPS, resolve
from mixsync.web.templating import templates

router = APIRouter(prefix="/review")
User = Annotated[AuthUser, Depends(require(Capability.request))]

_TEXT_MAX = 255
_MBID = re.compile(
    r"(?:https?://(?:www\.)?musicbrainz\.org/release/)?([0-9a-fA-F-]{36})(?:[/?#].*)?", re.S
)


def parse_release_mbid(text: str) -> str:
    m = _MBID.fullmatch(text.strip())
    try:
        if m:
            return str(uuid.UUID(m.group(1)))
    except ValueError:
        pass
    raise HTTPException(400, "not a MusicBrainz release URL or MBID")


def _owner(user: AuthUser) -> int | None:
    return None if Capability.approve in user.capabilities else user.id


def _target(d: MatchDecision, key: str) -> str:
    return str(Evidence.parse(d.evidence).target.get(key, "")).casefold()


_SORTS: dict[str, Callable[[MatchDecision], Any]] = {
    "date": lambda d: (d.created_at, d.id),
    "score": lambda d: d.distance,
    "artist": lambda d: _target(d, "artist"),
    "album": lambda d: _target(d, "title"),
    "match": lambda d: bool(d.release_mbid),
}


def _count(text: str) -> int:
    return int(text) if text.isascii() and text.isdigit() and len(text) <= 6 else 0


@router.get("")
def review_list(
    request: Request,
    user: User,
    db: Db,
    sort: str = "date",
    order: str = "asc",
    requeued: str | None = None,
    missing: str = "",
    busy: str = "",
    stale: str = "",
) -> HTMLResponse:
    items = decisions.pending_for(db, _owner(user))
    key = _SORTS.get(sort, _SORTS["date"])
    items.sort(key=key, reverse=order == "desc")
    ctx: dict[str, Any] = {"user": user, "items": items, "sort": sort, "order": order}
    if requeued is not None:  # the summary of a re-check, carried in the redirect
        n = {
            k: _count(v)
            for k, v in (
                ("requeued", requeued),
                ("missing", missing),
                ("busy", busy),
                ("stale", stale),
            )
        }
        ctx["n"] = n
    return templates.TemplateResponse(request, "pages/review_list.html", ctx)


@router.post("/recheck")
def recheck(user: User, db: Db) -> RedirectResponse:
    """Verify every pending review again with the current scorer: the decision is closed and the
    files go back through VERIFY against the release the request was given (a repick, or none
    for a migration that identified it). No network here."""
    now = datetime.now(UTC)
    n = Counter(
        review.recheck(db, now, d.id, user.id) for d in decisions.pending_for(db, _owner(user))
    )
    queued, missing, busy, stale = n["queued"], n["missing"], n["busy"], n["stale"]
    return RedirectResponse(
        f"/review?requeued={queued}&missing={missing}&busy={busy}&stale={stale}", status_code=303
    )


@router.get("/{decision_id}")
async def review_detail(
    request: Request, decision_id: int, user: User, db: Db, namer: Namer
) -> HTMLResponse:
    d = decision_for(db, user, decision_id)
    flags = _flags(request, db, user)  # before `resolve`, which ends the transaction
    refs = {("release", d.release_mbid)} | {
        ("recording", r) for r in Evidence.parse(d.evidence).acoustid_recording_ids
    }
    names = await resolve(db, namer, refs, MAX_LOOKUPS)
    ctx = {
        "user": user,
        "d": d,
        "prefill": _prefill(d),
        "names": names,
        **flags,
    }
    return templates.TemplateResponse(request, "pages/review_detail.html", ctx)


def _prefill(d: MatchDecision) -> dict[str, Any]:
    """Editable tags for Mark unverified: each downloaded file's own tags, else its file name."""
    files = Evidence.parse(d.evidence).files
    found: list[dict[str, str]] = []
    for f in files:
        try:
            found.append(tags.read_fields(Path(f["local_path"])))
        except Exception:  # missing or unreadable: the file name is all we have
            found.append({})
    rows: list[dict[str, str]] = []
    for i, (f, t) in enumerate(zip(files, found, strict=True), 1):
        disc, track, title = parse_name(Path(f.get("name") or "").stem)
        rows.append(
            {
                "name": f.get("name", ""),
                "title": t.get("title") or title,
                "artist": t.get("artist", ""),
                "disc": t.get("discnumber") or str(disc or 1),
                "track": t.get("tracknumber") or str(track or i),
            }
        )

    def first(key: str) -> str:
        return next((t[key] for t in found if t.get(key)), "")

    return {
        "albumartist": first("albumartist") or first("artist"),
        "album": first("album"),
        "year": first("date")[:4],
        "rows": rows,
    }


def _done(op: Callable[[], None], db: Db, user: AuthUser) -> RedirectResponse:
    try:
        op()
    except review.Refused as e:
        raise HTTPException(409, str(e)) from e
    return _next(db, user)


def _next(db: Db, user: AuthUser) -> RedirectResponse:
    """Open the next pending review, else the list."""
    pending = decisions.pending_for(db, _owner(user))
    return RedirectResponse(f"/review/{pending[0].id}" if pending else "/review", status_code=303)


@router.post("/{decision_id}/accept")
def accept(decision_id: int, user: User, db: Db) -> RedirectResponse:
    decision_for(db, user, decision_id)
    return _done(lambda: review.accept(db, datetime.now(UTC), decision_id, user.id), db, user)


@router.post("/{decision_id}/reject")
def reject(decision_id: int, user: User, db: Db) -> RedirectResponse:
    decision_for(db, user, decision_id)
    return _done(lambda: review.reject(db, datetime.now(UTC), decision_id, user.id), db, user)


@router.post("/{decision_id}/repick")
def repick(
    decision_id: int, user: User, db: Db, release: Annotated[str, Form()]
) -> RedirectResponse:
    decision_for(db, user, decision_id)
    mbid = parse_release_mbid(release)
    return _done(lambda: review.repick(db, datetime.now(UTC), decision_id, user.id, mbid), db, user)


def _int(text: str, what: str, minimum: int = 1) -> int:
    if not text.strip().isdecimal() or not minimum <= int(text) <= 9999:
        raise ValueError(f"{what} must be a whole number from {minimum} to 9999")
    return int(text)


def _text(text: str, what: str, required: bool = True) -> str:
    text = text.strip()
    if required and not text:
        raise ValueError(f"{what} is required")
    if len(text) > _TEXT_MAX:
        raise ValueError(f"{what} is at most {_TEXT_MAX} characters")
    if any(unicodedata.category(c) == "Cc" for c in text):
        raise ValueError(f"{what} must not contain control characters")
    return text


@router.post("/{decision_id}/unverified", response_model=None)
def unverified(
    request: Request,
    decision_id: int,
    user: User,
    db: Db,
    albumartist: Annotated[str, Form()],
    album: Annotated[str, Form()],
    title: Annotated[list[str], Form()],
    artist: Annotated[list[str], Form()],
    disc: Annotated[list[str], Form()],
    track: Annotated[list[str], Form()],
    year: Annotated[str, Form()] = "",
    deezer_album_id: Annotated[str, Form()] = "",
    discogs_release_id: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    d = decision_for(db, user, decision_id)
    files = Evidence.parse(d.evidence).files
    errors: list[str] = []

    def ok(fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
        try:
            return fn(*args, **kw)
        except ValueError as e:
            errors.append(str(e))

    if not len(files) == len(title) == len(artist) == len(disc) == len(track):
        errors.append("one set of fields per file is needed")
    edits = {
        "albumartist": ok(_text, albumartist, "album artist"),
        "album": ok(_text, album, "album"),
        "year": ok(_int, year, "year") if year.strip() else None,
        "tracks": [
            {
                "local_path": f.get("local_path", ""),
                "title": ok(_text, ti, f"{f.get('name', '')}: title"),
                "artist": ok(_text, ar, f"{f.get('name', '')}: artist", required=False),
                "disc": ok(_int, di, f"{f.get('name', '')}: disc"),
                "track": ok(_int, tr, f"{f.get('name', '')}: track"),
            }
            for f, ti, ar, di, tr in zip(files, title, artist, disc, track, strict=False)
        ],
    }
    if errors:  # show the form again with what was typed
        sent = _sent(
            files,
            *(albumartist, album, year, title, artist, disc, track),
            deezer_album_id,
            discogs_release_id,
        )
        ctx = {
            "user": user,
            "d": d,
            "prefill": sent,
            "errors": errors,
            **_flags(request, db, user),
        }
        return templates.TemplateResponse(request, "pages/review_detail.html", ctx, status_code=400)
    if _deezer_on(request) and deezer_album_id.isdecimal():  # the form came from a suggestion
        edits["suggestion"] = {"provider": "deezer", "album_id": int(deezer_album_id)}
    elif _discogs_on(request) and discogs_release_id.isdecimal():
        edits["suggestion"] = {"provider": "discogs", "release_id": int(discogs_release_id)}
    return _done(
        lambda: review.unverified(db, datetime.now(UTC), decision_id, user.id, edits), db, user
    )


def _deezer_on(request: Request) -> bool:
    return bool(request.app.state.services.settings.deezer_suggestions)


def _discogs_on(request: Request) -> bool:
    return bool(request.app.state.services.settings.discogs_suggestions)


def _discogs_token(request: Request, db: Db, user: AuthUser) -> str | None:
    """The reviewer's Discogs token, or None: setting off, not linked, or needs relinking."""
    if not _discogs_on(request):
        return None
    box = request.app.state.services.secret_box
    link = next((x for x in user_links(db, box, user.id) if x.service == "discogs"), None)
    return None if link is None or link.needs_relink else link_secret(box, link)


def _flags(request: Request, db: Db, user: AuthUser) -> dict[str, bool]:
    """Which suggestion providers this reviewer can use; the template shows nothing else."""
    return {
        "deezer": _deezer_on(request),
        "discogs": _discogs_token(request, db, user) is not None,
    }


def _sent(
    files: list[dict[str, Any]],
    albumartist: str,
    album: str,
    year: str,
    title: list[str],
    artist: list[str],
    disc: list[str],
    track: list[str],
    deezer_album_id: str,
    discogs_release_id: str,
) -> dict[str, Any]:
    """The form as submitted, to show it again."""
    return {
        "albumartist": albumartist,
        "album": album,
        "year": year,
        "deezer_album_id": deezer_album_id,
        "discogs_release_id": discogs_release_id,
        "rows": [
            {"name": f.get("name", ""), "title": ti, "artist": ar, "disc": di, "track": tr}
            for f, ti, ar, di, tr in zip(files, title, artist, disc, track, strict=False)
        ],
    }


async def _suggest_form(
    albumartist: Annotated[str, Form()] = "",
    album: Annotated[str, Form()] = "",
    year: Annotated[str, Form()] = "",
    title: Annotated[list[str], Form()] = [],  # noqa: B006
    artist: Annotated[list[str], Form()] = [],  # noqa: B006
    disc: Annotated[list[str], Form()] = [],  # noqa: B006
    track: Annotated[list[str], Form()] = [],  # noqa: B006
    deezer_album_id: Annotated[str, Form()] = "",
    discogs_release_id: Annotated[str, Form()] = "",
) -> tuple[str, str, str, list[str], list[str], list[str], list[str], str, str]:
    return (
        albumartist,
        album,
        year,
        title,
        artist,
        disc,
        track,
        deezer_album_id,
        discogs_release_id,
    )


SuggestForm = Annotated[
    tuple[str, str, str, list[str], list[str], list[str], list[str], str, str],
    Depends(_suggest_form),
]


@dataclass(frozen=True)
class Suggestion:
    provider: str  # "deezer" / "discogs": the choose route and form field prefix
    source: str  # the label shown
    id: int
    title: str
    artist: str
    year: int | None
    track_count: int | None


def _down(source: str) -> str:
    return (
        f"{source} could not be reached or gave no usable answer. "
        "You can still fill in the form by hand."
    )


def _release_lock(request: Request, db: Db, d: MatchDecision, token: str | None) -> dict[str, bool]:
    """End the request transaction before provider calls: the rate limiter writes to the same
    SQLite file. `d` stays readable for the page; the provider flags are read first."""
    flags = {"deezer": _deezer_on(request), "discogs": token is not None}
    db.expunge(d)
    db.commit()
    return flags


def _suggest_page(
    request: Request,
    flags: dict[str, bool],
    user: AuthUser,
    d: MatchDecision,
    prefill: dict[str, Any],
    **extra: Any,
) -> HTMLResponse:
    ctx = {"user": user, "d": d, "prefill": prefill, **flags, **extra}
    return templates.TemplateResponse(request, "pages/review_detail.html", ctx)


def _suggest_target(
    request: Request, db: Db, user: AuthUser, decision_id: int, provider: str | None
) -> tuple[MatchDecision, str | None]:
    """The pending review and the reviewer's Discogs token (None unless Discogs is available).
    `provider` None means the combined search: any available provider will do."""
    d = decision_for(db, user, decision_id)
    token = _discogs_token(request, db, user)
    have = {"deezer": _deezer_on(request), "discogs": token is not None}
    if not (have[provider] if provider else any(have.values())):
        raise HTTPException(404, "suggestions are off")
    if d.status != decisions.PENDING:
        raise HTTPException(409, "this review was already acted on")
    return d, token


async def _lookup(source: str, found: Awaitable[list[Suggestion]]) -> tuple[list[Suggestion], str]:
    try:
        hits = await found
    except (TransientError, PermanentError):
        return [], _down(source)
    return hits, "" if hits else f"{source} found no matching album."


async def _deezer_hits(deezer: Deezer, artist: str, album: str) -> list[Suggestion]:
    found = await deezer.search_albums(artist, album)
    return [
        Suggestion("deezer", "Deezer", a.id, a.title, a.artist, a.year, a.track_count)
        for a in found
    ]


async def _discogs_hits(discogs: Discogs, artist: str, album: str, token: str) -> list[Suggestion]:
    found = await discogs.search_releases(artist, album, token=token)
    return [Suggestion("discogs", "Discogs", r.id, r.title, r.artist, r.year, None) for r in found]


async def _search(
    request: Request,
    decision_id: int,
    user: AuthUser,
    db: Db,
    form: SuggestForm,
    provider: str | None,
    deezer: Deezer,
    discogs: Discogs,
) -> HTMLResponse:
    d, token = _suggest_target(request, db, user, decision_id, provider)
    prefill = _sent(Evidence.parse(d.evidence).files, *form)
    flags = _release_lock(request, db, d, token)
    jobs: list[tuple[str, Awaitable[list[Suggestion]]]] = []
    if provider in (None, "deezer") and _deezer_on(request):
        jobs.append(("Deezer", _deezer_hits(deezer, form[0], form[1])))
    if provider in (None, "discogs") and token is not None:
        jobs.append(("Discogs", _discogs_hits(discogs, form[0], form[1], token)))
    hits: list[Suggestion] = []
    notes: list[str] = []
    for source, job in jobs:  # one request per provider, one after the other
        found, note = await _lookup(source, job)
        hits += found
        notes.append(note)
    return _suggest_page(
        request, flags, user, d, prefill, suggestions=hits, note=" ".join(n for n in notes if n)
    )


@router.post("/{decision_id}/suggest")
async def suggest(
    request: Request,
    decision_id: int,
    user: User,
    db: Db,
    deezer: Deezer,
    discogs: Discogs,
    form: SuggestForm,
) -> HTMLResponse:
    """Search every available provider for the form's album artist and album."""
    return await _search(request, decision_id, user, db, form, None, deezer, discogs)


@router.post("/{decision_id}/deezer/search")
async def deezer_search(
    request: Request,
    decision_id: int,
    user: User,
    db: Db,
    deezer: Deezer,
    discogs: Discogs,
    form: SuggestForm,
) -> HTMLResponse:
    """Search Deezer for the form's album artist and album; the form comes back as typed."""
    return await _search(request, decision_id, user, db, form, "deezer", deezer, discogs)


_Chosen = DeezerAlbum | DiscogsRelease


async def _choose(
    request: Request,
    decision_id: int,
    user: AuthUser,
    db: Db,
    form: SuggestForm,
    provider: str,
    picked: str,
    fetch: Callable[[int, str | None], Awaitable[_Chosen]],
) -> HTMLResponse:
    """Fill the form from one album: rows match on disc and track number."""
    source = provider.capitalize()
    d, token = _suggest_target(request, db, user, decision_id, provider)
    if not picked.isdecimal() or len(picked) > 18:
        raise HTTPException(400, f"not a {source} id")
    prefill = _sent(Evidence.parse(d.evidence).files, *form)
    flags = _release_lock(request, db, d, token)
    try:
        chosen = await fetch(int(picked), token)
    except (TransientError, PermanentError):
        return _suggest_page(request, flags, user, d, prefill, note=_down(source))
    by_pos = {(t.disc, t.track): t for t in chosen.tracks}
    matched = 0
    for row in prefill["rows"]:
        pos = (_num(row["disc"]), _num(row["track"]))
        if (t := by_pos.get(pos)) is not None:
            row["title"], row["artist"] = t.title, t.artist
            matched += 1
    prefill["albumartist"], prefill["album"] = chosen.artist, chosen.title
    if chosen.year:
        prefill["year"] = str(chosen.year)
    prefill["deezer_album_id"] = prefill["discogs_release_id"] = ""  # one provenance at a time
    prefill["deezer_album_id" if provider == "deezer" else "discogs_release_id"] = str(chosen.id)
    note = f"{source} has {len(chosen.tracks)} tracks, {matched} matched."
    return _suggest_page(request, flags, user, d, prefill, note=note)


@router.post("/{decision_id}/deezer/choose")
async def deezer_choose(
    request: Request,
    decision_id: int,
    user: User,
    db: Db,
    deezer: Deezer,
    form: SuggestForm,
    deezer_album: Annotated[str, Form()],
) -> HTMLResponse:
    async def fetch(album_id: int, _token: str | None) -> _Chosen:
        return await deezer.album(album_id)

    return await _choose(request, decision_id, user, db, form, "deezer", deezer_album, fetch)


@router.post("/{decision_id}/discogs/choose")
async def discogs_choose(
    request: Request,
    decision_id: int,
    user: User,
    db: Db,
    discogs: Discogs,
    form: SuggestForm,
    discogs_album: Annotated[str, Form()],
) -> HTMLResponse:
    async def fetch(release_id: int, token: str | None) -> _Chosen:
        assert token is not None  # _suggest_target checked the link
        return await discogs.release(release_id, token=token)

    return await _choose(request, decision_id, user, db, form, "discogs", discogs_album, fetch)


def _num(text: str) -> int:
    return int(text) if text.strip().isdecimal() else 0
