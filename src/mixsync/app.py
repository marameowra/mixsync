from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from mixsync.db.engine import make_engine, make_session_factory
from mixsync.web.routes import auth, home


def create_app(session_factory: sessionmaker[Session] | None = None) -> FastAPI:
    app = FastAPI(title="mixsync")
    app.state.session_factory = session_factory or make_session_factory(make_engine())
    app.include_router(auth.router)
    app.include_router(home.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]  # registered by decorator
        return {"status": "ok"}

    return app
