"""The real mixsync app on a throwaway seeded SQLite database, with swappable themes."""

import os
import shutil
import socket
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from html import escape
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import BaseLoader, Environment, FileSystemLoader, TemplateNotFound

ROOT = Path(__file__).resolve().parents[2]
SECRET_KEY = "5cdn1297qyoYXdRm42ClFLpWl5dO-zdCYxebJ8O3av8="  # fixed: previews are throwaway
LOOPBACK = {"127.0.0.1", "::1", "localhost"}

theme_var: ContextVar[str] = ContextVar("theme", default="current")


def preview_env(root: Path) -> dict[str, str]:
    return {
        "MIXSYNC_DATABASE_URL": f"sqlite:///{root / 'preview.db'}",
        "MIXSYNC_CONFIG_DIR": str(root / "config"),
        "MIXSYNC_DATA_DIR": str(root / "data"),
        "MIXSYNC_DOWNLOADS_DIR": str(root / "downloads"),
        "MIXSYNC_IMPORT_SOURCE_DIR": str(root / "import-source"),
        "MIXSYNC_SECRET_KEY": SECRET_KEY,
    }


def block_network() -> None:
    """Like tests/conftest.py _block_sockets, but loopback stays open so uvicorn can serve."""
    if getattr(socket.socket.connect, "preview_guard", False):
        return
    real_connect, real_create = socket.socket.connect, socket.create_connection

    def allowed(address: Any) -> bool:
        return not isinstance(address, tuple) or address[0] in LOOPBACK

    def connect(self: socket.socket, address: Any) -> None:
        if not allowed(address):
            raise RuntimeError(f"preview: network access to {address} is blocked")
        real_connect(self, address)

    def create_connection(address: Any, *a: Any, **k: Any) -> socket.socket:
        if not allowed(address):
            raise RuntimeError(f"preview: network access to {address} is blocked")
        return real_create(address, *a, **k)

    connect.preview_guard = True  # type: ignore[attr-defined]
    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.create_connection = create_connection


class ThemeLoader(BaseLoader):
    """The active theme's templates first, then the real ones."""

    def __init__(self, fallback: BaseLoader, themes: Path) -> None:
        self.fallback, self.themes = fallback, themes

    def get_source(self, environment: Environment, template: str):  # noqa: ANN201
        try:
            theme = FileSystemLoader(self.themes / theme_var.get() / "templates")
            return theme.get_source(environment, template)
        except TemplateNotFound:
            return self.fallback.get_source(environment, template)


def _reset(root: Path) -> None:
    # The only delete in the preview: a throwaway dir this module owns, refused for any other name.
    if root.name != ".preview":
        raise RuntimeError(f"refusing to wipe {root}: not a .preview directory")
    if root.exists():
        shutil.rmtree(root)  # noqa: TID251
    root.mkdir(parents=True)


def _index(themes: list[str], pages: list[tuple[str, str]], current: str) -> str:
    users = "".join(
        f'<form method="post" action="/login" style="display:inline">'
        f'<input type="hidden" name="username" value="{u}">'
        f'<input type="hidden" name="password" value="preview">'
        f"<button>log in as {u}</button></form> "
        for u in ("admin", "alice", "edge")
    )
    theme_links = "".join(
        f'<li><a href="/__preview?theme={escape(t)}">{escape(t)}</a>'
        f"{' (active)' if t == current else ''}</li>"
        for t in themes
    )
    page_links = "".join(f'<li><a href="{escape(p)}">{escape(n)}</a></li>' for n, p in pages)
    return (
        "<!doctype html><title>UI preview</title><h1>UI preview</h1>"
        f"<h2>Theme</h2><ul>{theme_links}</ul>"
        f'<h2>Session</h2><p>{users}<a href="/__preview/logout">log out</a> '
        '| <a href="/login">login page</a></p>'
        f"<h2>Pages</h2><ul>{page_links}</ul>"
    )


def build_app(
    root: Path = ROOT / ".preview", themes_dir: Path = ROOT / "design" / "themes"
) -> FastAPI:
    _reset(root)
    os.environ.update(preview_env(root))
    block_network()
    # imported late: Settings and the migrations read the environment set above
    from mixsync.app import create_app
    from mixsync.core.config import Settings
    from mixsync.db.engine import make_engine, make_session_factory
    from mixsync.db.migrate import upgrade
    from mixsync.db.secretbox import load_box
    from mixsync.web.deps import SESSION_COOKIE, get_watch
    from mixsync.web.templating import templates

    from .fakes import FakeWatch
    from .seed import seed

    upgrade()  # alembic, not create_all: migration 0002 seeds the roles create_user needs
    settings = Settings()
    sf = make_session_factory(make_engine(settings))
    pages = seed(sf, load_box(settings), Path(settings.downloads_dir))

    env = templates.env
    fallback = env.loader.fallback if isinstance(env.loader, ThemeLoader) else env.loader
    assert fallback is not None
    env.loader = ThemeLoader(fallback, themes_dir)
    env.cache = None  # a cached template would leak from one theme into the next

    app = create_app(sf, settings)
    app.dependency_overrides[get_watch] = lambda: FakeWatch()
    app.mount("/themes", StaticFiles(directory=themes_dir), name="themes")

    def names() -> list[str]:
        return sorted(p.name for p in themes_dir.iterdir() if p.is_dir())

    @app.middleware("http")
    async def pick_theme(  # pyright: ignore[reportUnusedFunction]
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        asked = request.query_params.get("theme")
        chosen = asked if asked in names() else request.cookies.get("theme", "current")
        theme_var.set(chosen if chosen in names() else "current")
        response = await call_next(request)
        if asked in names():
            response.set_cookie("theme", str(asked))
        return response

    @app.get("/__preview")
    def switcher() -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        return HTMLResponse(_index(names(), pages, theme_var.get()))

    @app.get("/__preview/logout")
    def logout() -> RedirectResponse:  # pyright: ignore[reportUnusedFunction]
        resp = RedirectResponse("/__preview", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)  # the real logout is a CSRF-protected POST
        return resp

    return app
