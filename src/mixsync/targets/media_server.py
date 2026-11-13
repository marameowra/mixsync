"""The media server a deployment hands off to (ADR 0024): the one place a kind becomes adapters.
A new kind adds its adapters here and its name to config.MEDIA_SERVER_KINDS."""

from collections.abc import Callable

from mixsync.core.config import Settings
from mixsync.core.protocols import LibraryTarget, UserLibrary
from mixsync.ratelimit.client import PoliteClient
from mixsync.targets.navidrome import NavidromeTarget, NavidromeUser
from mixsync.targets.subsonic import SubsonicClient


def library_target(http: PoliteClient, settings: Settings) -> LibraryTarget:
    """The admin connection that rescans the library."""
    s = settings
    return NavidromeTarget(http, s.media_server_url, s.media_server_user, s.media_server_password)


def user_library(http: PoliteClient, settings: Settings) -> Callable[[str, str], UserLibrary]:
    """(username, password) -> that user's library on the media server."""
    url = settings.media_server_url
    return lambda user, password: NavidromeUser(http, url, user, password)


async def ping(http: PoliteClient, url: str, user: str, password: str) -> None:
    """One request as `user`; raises AuthError, PermanentError or TransientError."""
    await SubsonicClient(http, url, user, password).ping()
