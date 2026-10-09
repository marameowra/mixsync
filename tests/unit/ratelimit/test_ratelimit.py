from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.config import Settings
from mixsync.db.base import Base
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.models import infra  # noqa: F401
from mixsync.db.models.infra import RateBucket
from mixsync.ratelimit.bucket import Bucket, aware
from mixsync.ratelimit.client import PoliteClient, polite_client
from mixsync.ratelimit.services import limits


class FakeClock:
    def __init__(self) -> None:
        self.t = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


@pytest.fixture
def sessions(settings: Settings) -> sessionmaker[Session]:
    engine = make_engine(settings)
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def client(
    service: str,
    settings: Settings,
    sessions: sessionmaker[Session],
    clock: FakeClock,
    handler: httpx.MockTransport,
) -> PoliteClient:
    return polite_client(
        service, settings, sessions, clock=clock, transport=handler, sleep=clock.sleep
    )


def test_bucket_refill_and_wait(
    settings: Settings, sessions: sessionmaker[Session], clock: FakeClock
) -> None:
    b = Bucket("acoustid", limits("acoustid", settings), sessions, clock)  # 3/s, capacity 3
    assert [b.reserve() for _ in range(3)] == [timedelta(0)] * 3
    wait = b.reserve()
    assert wait == pytest.approx(timedelta(seconds=1 / 3), abs=timedelta(milliseconds=1))
    clock.t += timedelta(seconds=1 / 3)
    assert b.reserve() == timedelta(0)
    clock.t += timedelta(seconds=10)  # refill is capped at capacity
    assert [b.reserve() for _ in range(3)] == [timedelta(0)] * 3
    assert b.reserve() > timedelta(0)


def test_bucket_persists_across_sessions(
    settings: Settings, sessions: sessionmaker[Session], clock: FakeClock
) -> None:
    lim = limits("musicbrainz", settings)
    assert Bucket("musicbrainz", lim, sessions, clock).reserve() == timedelta(0)
    fresh = make_session_factory(make_engine(settings))
    assert Bucket("musicbrainz", lim, fresh, clock).reserve() == timedelta(seconds=1)


async def test_requests_are_spaced(
    settings: Settings, sessions: sessionmaker[Session], clock: FakeClock
) -> None:
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={"u": str(r.url)}))
    async with client("deezer", settings, sessions, clock, t) as c:
        start = clock.now()
        for i in range(3):
            await c.get(f"https://x.test/{i}")
    assert clock.now() - start == timedelta(seconds=2)  # first is free, then 1 req/s


async def test_retry_after_blocks_and_is_honored(
    settings: Settings, sessions: sessionmaker[Session], clock: FakeClock
) -> None:
    calls: list[datetime] = []

    def handler(_r: httpx.Request) -> httpx.Response:
        calls.append(clock.now())
        return (
            httpx.Response(429, headers={"Retry-After": "10"})
            if len(calls) == 1
            else (httpx.Response(200))
        )

    async with client("acoustid", settings, sessions, clock, httpx.MockTransport(handler)) as c:
        r = await c.get("https://x.test/")
        with sessions() as s:
            row = s.get(RateBucket, "acoustid")
            assert row is not None
            assert aware(row.blocked_until or clock.t) == calls[0] + timedelta(seconds=10)
    assert r.status_code == 200
    assert calls[1] - calls[0] == timedelta(seconds=10)


async def test_503_without_header_backs_off(
    settings: Settings, sessions: sessionmaker[Session], clock: FakeClock
) -> None:
    n = 0

    def handler(_r: httpx.Request) -> httpx.Response:
        nonlocal n
        n += 1
        return httpx.Response(503 if n < 3 else 200)

    async with client("musicbrainz", settings, sessions, clock, httpx.MockTransport(handler)) as c:
        start = clock.now()
        assert (await c.get("https://x.test/")).status_code == 200
    assert clock.now() - start >= timedelta(seconds=1.5)


async def test_cache_hit_and_expiry(
    settings: Settings, sessions: sessionmaker[Session], clock: FakeClock
) -> None:
    n = 0

    def handler(_r: httpx.Request) -> httpx.Response:
        nonlocal n
        n += 1
        return httpx.Response(200, json={"n": n})

    async with client("listenbrainz", settings, sessions, clock, httpx.MockTransport(handler)) as c:
        assert (await c.get("https://x.test/a")).json() == {"n": 1}
        assert (await c.get("https://x.test/a")).json() == {"n": 1}
        assert n == 1
        clock.t += timedelta(hours=1, seconds=1)  # ttl is 1 h
        assert (await c.get("https://x.test/a")).json() == {"n": 2}
        # authenticated requests are never cached
        await c.get("https://x.test/b", headers={"Authorization": "Token t"})
        await c.get("https://x.test/b", headers={"Authorization": "Token t"})
        assert n == 4


async def test_musicbrainz_search_ttl_is_shorter(
    settings: Settings, sessions: sessionmaker[Session], clock: FakeClock
) -> None:
    n = 0

    def handler(_r: httpx.Request) -> httpx.Response:
        nonlocal n
        n += 1
        return httpx.Response(200)

    async with client("musicbrainz", settings, sessions, clock, httpx.MockTransport(handler)) as c:
        await c.get("https://x.test/artist/1")
        await c.get("https://x.test/artist", params={"query": "q"})
        clock.t += timedelta(days=2)
        await c.get("https://x.test/artist/1")
        await c.get("https://x.test/artist", params={"query": "q"})
    assert n == 3


async def test_user_agent_is_mixsync(
    settings: Settings, sessions: sessionmaker[Session], clock: FakeClock
) -> None:
    seen: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r.headers["user-agent"])
        return httpx.Response(200)

    for service in ("slskd", "coverart"):
        async with client(service, settings, sessions, clock, httpx.MockTransport(handler)) as c:
            await c.get("https://x.test/")
    assert all(ua.startswith("MixSync/") and "github.com/marameowra/mixsync" in ua for ua in seen)
    assert len(seen) == 2
