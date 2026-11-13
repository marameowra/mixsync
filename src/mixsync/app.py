import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from mixsync.core.config import Settings
from mixsync.db.engine import user_sessions
from mixsync.library import tag_undo
from mixsync.web.csp import CSP
from mixsync.web.csrf import check_csrf
from mixsync.web.routes import (
    account,
    approvals,
    auth,
    connections,
    discover,
    home,
    library,
    media,
    migration,
    mirrored,
    requests,
    review,
    smart,
    spotify,
    trash,
    users,
    watchlist,
)
from mixsync.web.routes import settings as settings_routes
from mixsync.web.templating import templates
from mixsync.wiring import Services, build_services

log = logging.getLogger(__name__)


def create_app(
    session_factory: sessionmaker[Session] | None = None,
    settings: Settings | None = None,
    services: Services | None = None,
    stop_server: Callable[[], None] | None = None,
) -> FastAPI:
    """`stop_server` shuts the server down cleanly: Apply on the settings page calls it (#349)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # a tag restore killed with the web process leaves its track missing until this puts it back
        tag_undo.recover(app.state.session_factory, app.state.services.fileops)
        yield

    app = FastAPI(title="mixsync", dependencies=[Depends(check_csrf)], lifespan=lifespan)
    services = services or build_services(settings or Settings(), session_factory)
    app.state.services = services
    app.state.settings = services.settings
    app.state.stop_server = stop_server
    app.state.session_factory = user_sessions(services.session_factory)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"))

    @app.middleware("http")
    async def csp_and_timing(  # pyright: ignore[reportUnusedFunction]  # registered by decorator
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)  # a route may relax it
        # Until the body is sent: a streamed response logs its time to first byte.
        ms = (time.perf_counter() - start) * 1000
        log.debug("%s %s %s %.1fms", request.method, request.url.path, response.status_code, ms)
        return response

    @app.exception_handler(Exception)
    def server_error(request: Request, exc: Exception) -> Response:  # pyright: ignore[reportUnusedFunction]  # registered by decorator
        # ServerErrorMiddleware sits outside the CSP middleware, so this response sets its own
        return PlainTextResponse(
            "Internal Server Error", status_code=500, headers={"Content-Security-Policy": CSP}
        )

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException) -> Response:  # pyright: ignore[reportUnusedFunction]  # registered by decorator
        if exc.status_code < 400:  # e.g. the 303 to /login from current_user
            return Response(status_code=exc.status_code, headers=exc.headers)
        ctx = {"status": exc.status_code, "detail": exc.detail, "back": "/review"}
        if request.url.path == "/" or not request.url.path.startswith("/review"):
            ctx["back"] = "/"
        headers = dict(exc.headers or {})
        if "HX-Request" in request.headers and "HX-Boosted" not in request.headers:
            headers["HX-Reswap"] = "none"  # keep polling regions and Test buttons as they are
        elif request.method != "GET" and "HX-Boosted" in request.headers:
            headers["HX-Push-Url"] = "false"  # a rejected boosted form is not a new page
        return templates.TemplateResponse(
            request, "pages/error.html", ctx, status_code=exc.status_code, headers=headers
        )

    app.include_router(auth.router)
    app.include_router(account.router)
    app.include_router(home.router)
    app.include_router(review.router)
    app.include_router(requests.router)
    app.include_router(approvals.router)
    app.include_router(library.router)
    app.include_router(media.router)
    app.include_router(migration.router)
    app.include_router(users.router)
    app.include_router(trash.router)
    app.include_router(settings_routes.router)
    app.include_router(connections.router)
    app.include_router(watchlist.router)
    app.include_router(discover.router)
    app.include_router(mirrored.router)
    app.include_router(smart.router)
    app.include_router(spotify.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]  # registered by decorator
        return {"status": "ok"}

    return app
