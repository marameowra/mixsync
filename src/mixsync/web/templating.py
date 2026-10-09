from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from mixsync.web.csrf import csrf_token


def _context(request: Request) -> dict[str, str]:
    return {"csrf_token": csrf_token(request)}


templates = Jinja2Templates(
    directory=Path(__file__).parent / "templates", context_processors=[_context]
)
