"""Response cache in the `http_cache` table.

hishel was not used: it follows the server's Cache-Control/validators, but the README needs
forced per-service TTLs (MusicBrainz sends short or no cache headers), and it would add a
second storage layer next to the DB we already have.
"""

import hashlib
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session, sessionmaker

from mixsync.db.models.infra import HttpCache
from mixsync.ratelimit.bucket import aware

_SKIP_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}


def cacheable(request: httpx.Request) -> bool:
    return request.method == "GET" and "authorization" not in request.headers


def _key(request: httpx.Request) -> str:
    return hashlib.sha256(f"{request.method} {request.url}".encode()).hexdigest()


class Cache:
    def __init__(self, service: str, sessions: sessionmaker[Session]) -> None:
        self.service = service
        self.sessions = sessions

    def get(self, request: httpx.Request, now: datetime) -> httpx.Response | None:
        with self.sessions() as s:
            row = s.get(HttpCache, _key(request))
            if row is None or aware(row.expires_at) <= now:
                return None
            return httpx.Response(
                row.status, headers=row.headers, content=row.body, request=request
            )

    def put(
        self, request: httpx.Request, response: httpx.Response, now: datetime, ttl: timedelta
    ) -> None:
        headers = {k: v for k, v in response.headers.items() if k not in _SKIP_HEADERS}
        with self.sessions.begin() as s:
            s.merge(
                HttpCache(
                    key=_key(request),
                    service=self.service,
                    status=response.status_code,
                    headers=headers,
                    body=response.content,
                    fetched_at=now,
                    expires_at=now + ttl,
                )
            )
