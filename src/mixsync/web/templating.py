from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from mixsync.web.csrf import csrf_token


def _context(request: Request) -> dict[str, str]:
    return {"csrf_token": csrf_token(request)}


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


templates = Jinja2Templates(
    directory=Path(__file__).parent / "templates", context_processors=[_context]
)
templates.env.filters["filesize"] = filesize  # pyright: ignore[reportUnknownMemberType]
templates.env.filters["duration"] = duration  # pyright: ignore[reportUnknownMemberType]
