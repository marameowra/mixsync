import sqlite3
import threading
import time
from collections.abc import Iterator

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.config import Settings
from mixsync.web.deps import Db


def test_a_streamed_response_does_not_hold_the_write_lock(
    settings: Settings, sessions: sessionmaker[Session]
) -> None:
    """Every request writes (last_seen_at); a slow body, like an audio preview, must not keep
    that transaction open and lock the worker out."""
    app = FastAPI()
    app.state.session_factory = sessions

    @app.get("/slow")
    def slow(db: Db) -> StreamingResponse:  # pyright: ignore[reportUnusedFunction]  # registered by decorator
        db.execute(text("CREATE TABLE IF NOT EXISTS probe (x INTEGER)"))
        db.execute(text("INSERT INTO probe VALUES (1)"))

        def body() -> Iterator[bytes]:
            for _ in range(3):
                time.sleep(0.5)
                yield b"x"

        return StreamingResponse(body())

    with sessions.begin() as s:
        s.execute(text("CREATE TABLE IF NOT EXISTS probe (x INTEGER)"))
    seen: list[str] = []

    def writer() -> None:
        time.sleep(0.75)  # mid-stream
        c = sqlite3.connect(settings.database_url.removeprefix("sqlite:///"), timeout=0.2)
        try:
            c.execute("INSERT INTO probe VALUES (2)")
            c.commit()
            seen.append("ok")
        except sqlite3.OperationalError as e:
            seen.append(str(e))

    t = threading.Thread(target=writer)
    t.start()
    assert TestClient(app).get("/slow").status_code == 200
    t.join()
    assert seen == ["ok"]
