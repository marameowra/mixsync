import re
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from mixsync.web.csrf import csrf_token


def _context(request: Request) -> dict[str, Any]:
    return {
        "csrf_token": csrf_token(request),
        "waiting_counts": getattr(request.state, "waiting_counts", {}),
    }


def filesize(n: float | None) -> str:
    """Bytes as MB or GB, 1 decimal, base 1000 (matches what file managers and slskd show)."""
    if n is None:
        return "–"
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB"


def duration(sec: float | None) -> str:
    """Seconds as m:ss, or h:mm:ss from one hour."""
    if sec is None:
        return "–"
    m, s = divmod(round(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


_ARTWORK_SRC = re.compile(
    r"(?:/media/cover/[1-9][0-9]*|/media/artwork/release/"
    r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}"
    r"(?:\?group=[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})?"
    r"|/media/artwork/release-group/"
    r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})\Z"
)


def _valid_artwork_src(src: object) -> bool:
    return isinstance(src, str) and bool(_ARTWORK_SRC.fullmatch(src))


templates = Jinja2Templates(
    directory=Path(__file__).parent / "templates", context_processors=[_context]
)
templates.env.filters["filesize"] = filesize  # pyright: ignore[reportUnknownMemberType]
templates.env.filters["duration"] = duration  # pyright: ignore[reportUnknownMemberType]
templates.env.tests["valid_artwork_src"] = _valid_artwork_src  # pyright: ignore[reportUnknownMemberType]


def render(
    request: Request, page: str, fragment: str, context: dict[str, Any] | None = None
) -> HTMLResponse:
    """The fragment template for an HTMX request, the full page otherwise. Routes keep their own
    auth dependencies, so permissions and CSRF are the same either way."""
    name = fragment if request.headers.get("HX-Request") == "true" else page
    return templates.TemplateResponse(request, name, context or {})
