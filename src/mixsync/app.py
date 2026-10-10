from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from mixsync.core.config import Settings
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.web.csrf import check_csrf
from mixsync.web.routes import auth, home, media, requests, review
from mixsync.web.templating import templates


def create_app(
    session_factory: sessionmaker[Session] | None = None, settings: Settings | None = None
) -> FastAPI:
    app = FastAPI(title="mixsync", dependencies=[Depends(check_csrf)])
    app.state.settings = settings or Settings()
    app.state.session_factory = session_factory or make_session_factory(make_engine())
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"))

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException) -> Response:  # pyright: ignore[reportUnusedFunction]  # registered by decorator
        if exc.status_code < 400:  # e.g. the 303 to /login from current_user
            return Response(status_code=exc.status_code, headers=exc.headers)
        ctx = {"status": exc.status_code, "detail": exc.detail, "back": "/review"}
        if request.url.path == "/" or not request.url.path.startswith("/review"):
            ctx["back"] = "/"
        return templates.TemplateResponse(
            request, "pages/error.html", ctx, status_code=exc.status_code, headers=exc.headers
        )

    app.include_router(auth.router)
    app.include_router(home.router)
    app.include_router(review.router)
    app.include_router(requests.router)
    app.include_router(media.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]  # registered by decorator
        return {"status": "ok"}

    return app
