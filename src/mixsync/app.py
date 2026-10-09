from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.config import Settings
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.web.csrf import check_csrf
from mixsync.web.routes import auth, home, media, review


def create_app(
    session_factory: sessionmaker[Session] | None = None, settings: Settings | None = None
) -> FastAPI:
    app = FastAPI(title="mixsync", dependencies=[Depends(check_csrf)])
    app.state.settings = settings or Settings()
    app.state.session_factory = session_factory or make_session_factory(make_engine())
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"))
    app.include_router(auth.router)
    app.include_router(home.router)
    app.include_router(review.router)
    app.include_router(media.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]  # registered by decorator
        return {"status": "ok"}

    return app
