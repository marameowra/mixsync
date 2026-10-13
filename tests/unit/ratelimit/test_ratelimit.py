import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import aware
from mixsync.core.config import Settings
from mixsync.db.base import Base
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.models import infra  # noqa: F401
from mixsync.db.models.infra import RateBucket
from mixsync.ratelimit.bucket import Bucket
from mixsync.ratelimit.client import _BACKOFF_CAP, PoliteClient, polite_client
from mixsync.ratelimit.services import ServiceLimits, limits


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


async def test_reset_in_blocks_when_remaining_is_zero(
    settings: Settings, sessions: sessionmaker[Session], clock: FakeClock
) -> None:
    calls: list[datetime] = []

    def handler(_r: httpx.Request) -> httpx.Response:
        calls.append(clock.now())
        headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-In": "7"}
        return httpx.Response(200, headers=headers if len(calls) == 1 else {})

    async with client("listenbrainz", settings, sessions, clock, httpx.MockTransport(handler)) as c:
        await c.get("https://x.test/a")
        await c.get("https://x.test/b")
    assert calls[1] - calls[0] == timedelta(seconds=7)


async def test_429_without_retry_after_uses_reset_in(
    settings: Settings, sessions: sessionmaker[Session], clock: FakeClock
) -> None:
    calls: list[datetime] = []

    def handler(_r: httpx.Request) -> httpx.Response:
        calls.append(clock.now())
        if len(calls) == 1:
            return httpx.Response(429, headers={"X-RateLimit-Reset-In": "12"})
        return httpx.Response(200)

    async with client("listenbrainz", settings, sessions, clock, httpx.MockTransport(handler)) as c:
        r = await c.get("https://x.test/")
    assert r.status_code == 200
    assert calls[1] - calls[0] == timedelta(seconds=12)


async def gap_after_429(
    settings: Settings,
    sessions: sessionmaker[Session],
    clock: FakeClock,
    headers: dict[str, str],
) -> timedelta:
    calls: list[datetime] = []

    def handler(_r: httpx.Request) -> httpx.Response:
        calls.append(clock.now())
        return httpx.Response(429, headers=headers) if len(calls) == 1 else httpx.Response(200)

    async with client("listenbrainz", settings, sessions, clock, httpx.MockTransport(handler)) as c:
        await c.get("https://x.test/")
    return calls[1] - calls[0]


@pytest.mark.parametrize("value", ["-5", "0", "soon", ""])
async def test_unusable_reset_in_falls_back_to_backoff(
    settings: Settings, sessions: sessionmaker[Session], clock: FakeClock, value: str
) -> None:
    gap = await gap_after_429(settings, sessions, clock, {"X-RateLimit-Reset-In": value})
    assert timedelta(seconds=0.5) <= gap <= timedelta(seconds=1)


async def test_retry_after_wins_over_reset_in(
    settings: Settings, sessions: sessionmaker[Session], clock: FakeClock
) -> None:
    headers = {"Retry-After": "3", "X-RateLimit-Reset-In": "9"}
    assert await gap_after_429(settings, sessions, clock, headers) == timedelta(seconds=3)


@pytest.mark.parametrize("value", ["86400", "Fri, 01 Jan 2027 00:00:00 GMT"])
async def test_huge_retry_after_is_capped(
    settings: Settings, sessions: sessionmaker[Session], clock: FakeClock, value: str
) -> None:
    gap = await gap_after_429(settings, sessions, clock, {"Retry-After": value})
    assert gap == timedelta(seconds=_BACKOFF_CAP)


async def test_small_retry_after_is_exact(
    settings: Settings, sessions: sessionmaker[Session], clock: FakeClock
) -> None:
    assert await gap_after_429(settings, sessions, clock, {"Retry-After": "5"}) == timedelta(
        seconds=5
    )


async def test_429_with_remaining_zero_waits_reset_in(
    settings: Settings, sessions: sessionmaker[Session], clock: FakeClock
) -> None:
    headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-In": "4"}
    assert await gap_after_429(settings, sessions, clock, headers) == timedelta(seconds=4)


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


def test_two_engines_never_grant_inside_one_window(
    settings: Settings, sessions: sessionmaker[Session], clock: FakeClock
) -> None:
    lim = limits("musicbrainz", settings)  # 1 req/s
    other = make_session_factory(make_engine(settings))  # separate engine, same file
    buckets = [Bucket("musicbrainz", lim, f, clock) for f in (sessions, other)]
    grants: list[datetime] = []
    for i in range(10):
        b = buckets[i % 2]
        while (wait := b.reserve()) > timedelta(0):
            clock.t += wait
        grants.append(clock.now())
    assert all(b - a >= timedelta(seconds=1) for a, b in zip(grants, grants[1:], strict=False))


def test_bucket_picks_up_changed_rate(
    settings: Settings, sessions: sessionmaker[Session], clock: FakeClock
) -> None:
    fast = limits("acoustid", settings)  # 3/s
    old = Bucket("acoustid", fast, sessions, clock)
    assert [old.reserve() for _ in range(3)] == [timedelta(0)] * 3
    slow = Bucket("acoustid", ServiceLimits(rate=0.5), sessions, clock)
    assert slow.reserve() == pytest.approx(timedelta(seconds=2), abs=timedelta(milliseconds=1))
    with sessions() as s:
        row = s.get(RateBucket, "acoustid")
        assert row is not None
        assert row.refill_per_sec == 0.5


def test_aware_converts_to_same_instant() -> None:
    plus2 = datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=2)))
    assert aware(plus2) == plus2 and aware(plus2).utcoffset() == timedelta(0)
    assert aware(datetime(2026, 1, 1, 10)) == datetime(2026, 1, 1, 10, tzinfo=UTC)


def test_first_use_race_does_not_raise(
    settings: Settings, sessions: sessionmaker[Session], clock: FakeClock
) -> None:
    """Concurrent first reserves on a missing row: on Postgres FOR UPDATE locks nothing."""
    n = 3  # acoustid capacity
    barrier = threading.Barrier(n)

    def go() -> timedelta:
        b = Bucket("acoustid", limits("acoustid", settings), sessions, clock)
        barrier.wait()
        return b.reserve()

    with ThreadPoolExecutor(n) as pool:
        waits = [f.result() for f in [pool.submit(go) for _ in range(n)]]
    assert waits == [timedelta(0)] * n
