from typing import Any

from mixsync.ratelimit.client import PoliteClient
from mixsync.targets.subsonic import NotAuthorizedError, NotFoundError, SubsonicClient

_USER_HINT = "check your linked Navidrome account in mixsync"


class NavidromeTarget:
    def __init__(self, client: PoliteClient, base_url: str, user: str, password: str) -> None:
        self._subsonic = SubsonicClient(client, base_url, user, password)

    async def rescan(self) -> None:
        await self._subsonic.start_scan()


class NavidromeUser:
    """Per-user Navidrome calls (playlists, stars, play counts); stars and counts are per user."""

    def __init__(self, client: PoliteClient, base_url: str, user: str, password: str) -> None:
        self._subsonic = SubsonicClient(client, base_url, user, password, _USER_HINT, _USER_HINT)

    async def push_playlist(self, name: str, song_ids: list[str], existing_id: str | None) -> str:
        if existing_id:
            try:
                await self._subsonic.create_playlist(name, song_ids, existing_id)
                await self._subsonic.update_playlist(existing_id, name)  # renames reach Navidrome
                return existing_id
            except (NotFoundError, NotAuthorizedError):
                pass  # deleted (70) or no longer ours (50); make a new one
        return await self._subsonic.create_playlist(name, song_ids)

    async def starred_song_ids(self) -> set[str]:
        songs: list[dict[str, Any]] = (await self._subsonic.get_starred2()).get("song") or []
        return {str(s["id"]) for s in songs}

    async def play_counts(self, song_ids: list[str]) -> dict[str, int]:
        """Counts for song_ids; 0 when unreported, and absent when Navidrome no longer knows the
        song (a reset or rescan changed its id)."""
        counts: dict[str, int] = {}
        for i in song_ids:
            try:
                counts[i] = int((await self._subsonic.get_song(i)).get("playCount") or 0)
            except NotFoundError:
                continue  # deleted, or rescanned under a new id: not the same as 0 plays
        return counts

    async def music_folder_id(self, name: str) -> str | None:
        """Id of the library called `name` (e.g. the discover library), if this user can see it."""
        for folder in await self._subsonic.get_music_folders():
            if folder.get("name") == name:
                return str(folder["id"])
        return None

    async def resolve(
        self, recording_mbid: str, title: str, artist: str, music_folder_id: str | None = None
    ) -> str | None:
        """Song id whose musicBrainzId is `recording_mbid`, else None (never guesses).

        Not path matching: Navidrome reports a made-up Subsonic `path` unless real paths are
        enabled. Navidrome's song `musicBrainzId` is the recording MBID (verified on a live
        server, 2026-10-12).
        """
        for song in await self._subsonic.search3(
            f"{artist} {title}", music_folder_id=music_folder_id
        ):
            if song.get("musicBrainzId") == recording_mbid:
                return str(song["id"])
        return None
