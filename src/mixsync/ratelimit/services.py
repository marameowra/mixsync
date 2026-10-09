from dataclasses import dataclass
from datetime import timedelta
from importlib.metadata import version

import httpx

from mixsync.core.config import Settings

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
WEEK = timedelta(days=7)


@dataclass(frozen=True)
class ServiceLimits:
    rate: float | None  # requests per second; None = unlimited (local service)
    ttl: timedelta | None = None  # None = never cached
    search_ttl: timedelta | None = None  # shorter TTL for requests with a `query` param
    follow_redirects: bool = False


def limits(service: str, settings: Settings) -> ServiceLimits:
    table = {
        "musicbrainz": ServiceLimits(settings.mb_rate, WEEK, DAY),
        "coverart": ServiceLimits(1.0, follow_redirects=True),  # art is saved to disk instead
        "acoustid": ServiceLimits(3.0, WEEK),
        "listenbrainz": ServiceLimits(1.0, HOUR),
        "discogs": ServiceLimits(50 / 60, DAY),
        "deezer": ServiceLimits(1.0, DAY),
        "slskd": ServiceLimits(None),
        "navidrome": ServiceLimits(None),
    }
    return table[service]


def user_agent(settings: Settings) -> str:
    return f"MixSync/{version('mixsync')} ( {settings.mb_contact} )"


def ttl_for(lim: ServiceLimits, request: httpx.Request) -> timedelta | None:
    if lim.search_ttl is not None and "query" in request.url.params:
        return lim.search_ttl
    return lim.ttl
