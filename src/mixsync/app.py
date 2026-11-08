from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from mixsync.core.config import Settings
from mixsync.db.engine import user_sessions
from mixsync.library import tag_undo
from mixsync.web.csrf import check_csrf
from mixsync.web.routes import (
    account,
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
    users,
    watchlist,
)
from mixsync.web.routes import settings as settings_routes
from mixsync.web.templating import templates
from mixsync.wiring import Services, build_services


def create_app(
    session_factory: sessionmaker[Session] | None = None,
    settings: Settings | None = None,
    services: Services | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # a tag restore killed with the web process leaves its track missing until this puts it back
        tag_undo.recover(app.state.session_factory, app.state.services.fileops)
        yield

    app = FastAPI(title="mixsync", dependencies=[Depends(check_csrf)], lifespan=lifespan)
    services = services or build_services(settings or Settings(), session_factory)
    app.state.services = services
    app.state.settings = services.settings
    app.state.session_factory = user_sessions(services.session_factory)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"))

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
    app.include_router(library.router)
    app.include_router(media.router)
    app.include_router(migration.router)
    app.include_router(users.router)
    app.include_router(settings_routes.router)
    app.include_router(connections.router)
    app.include_router(watchlist.router)
    app.include_router(discover.router)
    app.include_router(mirrored.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]  # registered by decorator
        return {"status": "ok"}

    return app
