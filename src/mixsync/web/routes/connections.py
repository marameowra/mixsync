"""Test-connection buttons on the settings page: one lightweight request to a service with the
values in its form fields (unsaved ones included), through PoliteClient. Saves nothing."""

import asyncio
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from markupsafe import escape

from mixsync.core.capabilities import Capability
from mixsync.core.errors import AuthError, PermanentError, TransientError
from mixsync.db.app_settings import SECRETS, resolve
from mixsync.db.auth import AuthUser
from mixsync.metadata.acoustid import AcoustIdClient
from mixsync.metadata.musicbrainz import MusicBrainzProvider
from mixsync.ratelimit.client import PoliteClient
from mixsync.sources.slskd import SlskdSource
from mixsync.targets.subsonic import SubsonicClient
from mixsync.web.deps import Db, require

router = APIRouter(prefix="/settings")
Admin = Annotated[AuthUser, Depends(require(Capability.admin))]

TIMEOUT = 10.0  # seconds for the whole click, a rate-limit wait included
# service -> the form fields it reads; a blank field means its effective value
FIELDS = {
    "slskd": ("slskd_url", "slskd_api_key"),
    "navidrome": ("navidrome_url", "navidrome_user", "navidrome_password"),
    "musicbrainz": ("mb_base_url",),
    "acoustid": ("acoustid_app_key",),
}


async def _ping(service: str, v: dict[str, str], http: PoliteClient, request: Request) -> None:
    settings = request.app.state.settings
    if service == "slskd":
        downloads = Path(settings.downloads_dir)
        await SlskdSource(http, v["slskd_url"], v["slskd_api_key"], downloads_dir=downloads).ping()
    elif service == "navidrome":
        user, password = v["navidrome_user"], v["navidrome_password"]
        await SubsonicClient(http, v["navidrome_url"], user, password).ping()
    elif service == "musicbrainz":
        await MusicBrainzProvider(http, v["mb_base_url"]).ping()
    else:
        key = v["acoustid_app_key"]
        await AcoustIdClient(http, settings.model_copy(update={"acoustid_app_key": key})).ping()


def _cause(e: BaseException) -> BaseException | None:
    # clients wrap transport errors; Subsonic drops the cause (`from None`) but keeps the context
    return e.__cause__ or e.__context__


def _problem(service: str, v: dict[str, str]) -> str | None:
    """A form problem that makes the request pointless, or None."""
    url = next((v[n] for n in FIELDS[service] if n.endswith("_url")), None)
    if url is not None and not url.startswith(("http://", "https://")):
        return "Bad URL: it must start with http:// or https://."
    missing = [n for n in FIELDS[service] if not n.endswith("_url") and not v[n]]
    if service != "slskd" and missing:  # slskd may run without auth
        return f"Set {' and '.join(missing)} first."
    return None


async def _check(service: str, v: dict[str, str], request: Request) -> str | None:
    """None when the service answered OK, else the error to show."""
    if problem := _problem(service, v):
        return problem
    state = request.app.state
    transport = getattr(state, "connection_transport", None)  # tests inject a fake network
    try:
        async with PoliteClient(
            service, state.settings, state.services.session_factory, transport=transport
        ) as http:
            async with asyncio.timeout(TIMEOUT):
                await _ping(service, v, http, request)
    except TimeoutError:
        return "Timed out."
    except AuthError:  # the text is not shown: Subsonic's echoes the server and names env vars
        return "Authentication failed: check the credentials."
    except TransientError as e:
        if isinstance(_cause(e), httpx.TimeoutException):
            return "Timed out."
        if isinstance(_cause(e), httpx.TransportError):
            return "Unreachable: nothing answered there."
        return f"Failed: {e}."  # a 5xx or repeated 429/503; the messages carry no secrets
    except ValueError:  # e.g. slskd's URL answered 200 with an HTML page
        return "Failed: the answer was not JSON; is this the right URL?"
    except PermanentError as e:
        return f"Failed: {e}."
    except httpx.InvalidURL:
        return "Bad URL."
    return None


@router.post("/test/{service}")
async def check(service: str, request: Request, admin: Admin, db: Db) -> HTMLResponse:
    """The htmx fragment for a Test button: OK or the error."""
    if service not in FIELDS:
        raise HTTPException(404)
    form = await request.form()
    resolved = resolve(db)[1]
    values: dict[str, str] = {}
    for name in FIELDS[service]:
        raw = str(form.get(name, ""))
        if not raw.strip():
            values[name] = str(resolved[name].value or "")
        else:  # a secret is used exactly, as Save would store it
            values[name] = raw if name in SECRETS else raw.strip()
    error = await _check(service, values, request)
    if error:
        return HTMLResponse(f'<span class="error">{escape(error)}</span>')
    return HTMLResponse('<span class="tag confident">OK</span>')
