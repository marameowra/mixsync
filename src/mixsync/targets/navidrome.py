from typing import Any, Self

from mixsync.ratelimit.client import PoliteClient
from mixsync.targets.subsonic import NotFoundError, SubsonicClient

_USER_HINT = "check your linked Navidrome account in mixsync"


class NavidromeTarget:
    def __init__(self, client: PoliteClient, base_url: str, user: str, password: str) -> None:
        self._subsonic = SubsonicClient(client, base_url, user, password)

    async def rescan(self) -> None:
        await self._subsonic.start_scan()


class NavidromeUser:
    """Per-user Navidrome calls (playlists, stars, play counts); stars and counts are per user."""

    def __init__(self, client: PoliteClient, base_url: str, user: str, password: str) -> None:
        self._subsonic = SubsonicClient(client, base_url, user, password, _USER_HINT)

    @classmethod
    def for_user(cls, client: PoliteClient, base_url: str, username: str, password: str) -> Self:
        return cls(client, base_url, username, password)

    async def push_playlist(self, name: str, song_ids: list[str], existing_id: str | None) -> str:
        if existing_id:
            try:
                return await self._subsonic.create_playlist(name, song_ids, existing_id)
            except NotFoundError:
                pass  # deleted in Navidrome; make a new one
        return await self._subsonic.create_playlist(name, song_ids)

    async def starred_song_ids(self) -> set[str]:
        songs: list[dict[str, Any]] = (await self._subsonic.get_starred2()).get("song") or []
        return {str(s["id"]) for s in songs}

    async def play_counts(self, song_ids: list[str]) -> dict[str, int]:
        return {i: int((await self._subsonic.get_song(i)).get("playCount") or 0) for i in song_ids}

    async def resolve(self, path: str, title: str, artist: str) -> str | None:
        """Song id whose `path` equals `path`, else None.

        Docs call `path` "the full path of the media" and don't say it is library-relative, so
        pass it as Navidrome sees it (e.g. /data/discover/...); that also disambiguates libraries.
        """
        for song in await self._subsonic.search3(f"{artist} {title}"):
            if song.get("path") == path:
                return str(song["id"])
        return None
