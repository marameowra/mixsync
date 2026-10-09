from collections.abc import Callable

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.config import Settings
from mixsync.db.base import Base
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.models import infra  # noqa: F401
from mixsync.ratelimit.client import PoliteClient, polite_client


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def sessions(settings: Settings) -> sessionmaker[Session]:
    engine = make_engine(settings)
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


@pytest.fixture
def make_client(
    settings: Settings, sessions: sessionmaker[Session]
) -> Callable[[str, Callable[[httpx.Request], httpx.Response]], PoliteClient]:
    def make(service: str, handler: Callable[[httpx.Request], httpx.Response]) -> PoliteClient:
        return polite_client(
            service, settings, sessions, transport=httpx.MockTransport(handler), sleep=_no_sleep
        )

    return make
