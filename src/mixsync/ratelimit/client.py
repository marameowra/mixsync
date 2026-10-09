import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import timedelta
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Self

import httpx
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import Clock, SystemClock
from mixsync.core.config import Settings
from mixsync.core.errors import TransientError
from mixsync.ratelimit.bucket import Bucket
from mixsync.ratelimit.cache import Cache, cacheable
from mixsync.ratelimit.services import limits, ttl_for, user_agent

_BACKOFF_CAP = 300.0
_MAX_ATTEMPTS = 5


class PoliteClient:
    """The only way to make outbound HTTP: rate limited, cached, identified, backed off."""

    def __init__(
        self,
        service: str,
        settings: Settings,
        sessions: sessionmaker[Session],
        *,
        clock: Clock | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        **client_kwargs: Any,
    ) -> None:
        self.lim = limits(service, settings)
        self.clock = clock or SystemClock()
        self._sleep = sleep
        self.bucket = Bucket(service, self.lim, sessions, self.clock)
        self.cache = Cache(service, sessions)
        headers = {"User-Agent": user_agent(settings), **client_kwargs.pop("headers", {})}
        self._http = httpx.AsyncClient(
            headers=headers,
            transport=transport,
            follow_redirects=self.lim.follow_redirects,
            timeout=30,
            **client_kwargs,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        request = self._http.build_request(method, url, **kwargs)
        ttl = ttl_for(self.lim, request) if cacheable(request) else None
        if ttl is not None and (hit := self.cache.get(request, self.clock.now())):
            return hit
        for attempt in range(_MAX_ATTEMPTS):
            while (wait := self.bucket.reserve()) > timedelta(0):
                await self._sleep(wait.total_seconds())
            response = await self._http.send(request)
            if response.status_code not in (429, 503):
                if ttl is not None and response.status_code == 200:
                    self.cache.put(request, response, self.clock.now(), ttl)
                return response
            self.bucket.block(self._delay(response, attempt))
        raise TransientError(f"{request.url.host} still refusing after {_MAX_ATTEMPTS} attempts")

    def _delay(self, response: httpx.Response, attempt: int) -> timedelta:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            if retry_after.isdigit():
                return timedelta(seconds=int(retry_after))
            try:
                return max(timedelta(0), parsedate_to_datetime(retry_after) - self.clock.now())
            except ValueError:
                pass
        return timedelta(seconds=min(_BACKOFF_CAP, 2.0**attempt) * random.uniform(0.5, 1.0))


def polite_client(
    service: str, settings: Settings, sessions: sessionmaker[Session], **kwargs: Any
) -> PoliteClient:
    return PoliteClient(service, settings, sessions, **kwargs)
