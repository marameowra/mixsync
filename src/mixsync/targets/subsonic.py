import hashlib
import secrets
from dataclasses import dataclass
from typing import Any

import httpx

from mixsync.core.errors import AuthError, PermanentError, TransientError
from mixsync.ratelimit.client import PoliteClient

_AUTH_CODES = {40, 41}
_NOT_AUTHORIZED = 50
_NOT_FOUND = 70

_ADMIN_HINT = "check MIXSYNC_NAVIDROME_USER and its password"
_ADMIN_ONLY = "MIXSYNC_NAVIDROME_USER must be an admin user to start scans"


class NotFoundError(PermanentError):
    """Subsonic error 70: the requested item does not exist."""


class NotAuthorizedError(PermanentError):
    """Subsonic error 50: the user may not do this (e.g. someone else's playlist)."""


def _dig(body: dict[str, Any], key: str) -> dict[str, Any]:
    value: dict[str, Any] = body.get(key) or {}
    return value


@dataclass(frozen=True)
class ScanStatus:
    scanning: bool
    count: int | None


class SubsonicClient:
    def __init__(
        self,
        client: PoliteClient,
        base_url: str,
        user: str,
        password: str,
        credentials_hint: str = _ADMIN_HINT,
        forbidden_hint: str = _ADMIN_ONLY,
    ) -> None:
        self._hint = credentials_hint
        self._forbidden_hint = forbidden_hint
        self._client = client
        self._base = base_url.rstrip("/")
        self._user = user
        self._password = password

    def _auth(self) -> dict[str, str]:
        salt = secrets.token_hex(8)
        token = hashlib.md5((self._password + salt).encode()).hexdigest()  # noqa: S324 - Subsonic protocol
        return {"u": self._user, "t": token, "s": salt, "v": "1.16.1", "c": "mixsync", "f": "json"}

    async def _call(self, endpoint: str, *, post: bool = False, **params: Any) -> dict[str, Any]:
        url = f"{self._base}/rest/{endpoint}"
        try:
            merged = {**self._auth(), **params}
            if post:
                # OpenSubsonic formPost extension; keeps long songId lists and t/s out of URLs
                response = await self._client.post(url, data=merged)
            else:
                response = await self._client.get(url, params=merged)
        except httpx.TransportError as e:
            # str(e) can embed the request URL; the query holds the token, so name the type only
            raise TransientError(f"Subsonic {endpoint} unreachable ({type(e).__name__})") from None
        if response.status_code >= 500:
            raise TransientError(f"Subsonic {endpoint} returned {response.status_code}")
        if response.status_code != 200:
            raise PermanentError(f"Subsonic {endpoint} returned {response.status_code}")
        try:
            body: dict[str, Any] = response.json()["subsonic-response"]
        except (ValueError, KeyError, TypeError) as e:
            raise PermanentError(f"Subsonic {endpoint} gave an unusable response") from e
        if body.get("status") != "ok":
            error: dict[str, Any] = body.get("error") or {}
            code, message = error.get("code"), error.get("message")
            if code in _AUTH_CODES:
                raise AuthError(f"Navidrome rejected the credentials ({message}); {self._hint}")
            if code == _NOT_FOUND:
                raise NotFoundError(f"Subsonic {endpoint}: not found ({message})")
            if code == _NOT_AUTHORIZED:
                raise NotAuthorizedError(
                    f"Navidrome user is not authorized for {endpoint} ({message}); "
                    f"{self._forbidden_hint}"
                )
            raise PermanentError(f"Subsonic {endpoint} failed: code {code}: {message}")
        return body

    async def ping(self) -> None:
        await self._call("ping")

    async def start_scan(self, full: bool = False) -> None:
        await self._call("startScan", fullScan="true" if full else "false")

    async def get_scan_status(self) -> ScanStatus:
        status: dict[str, Any] = (await self._call("getScanStatus")).get("scanStatus") or {}
        return ScanStatus(bool(status.get("scanning")), status.get("count"))

    async def create_playlist(
        self, name: str, song_ids: list[str], playlist_id: str | None = None
    ) -> str:
        # With playlistId the docs don't say replace vs append; Navidrome replaces the tracks.
        params: dict[str, Any] = {"songId": song_ids}
        params.update({"playlistId": playlist_id} if playlist_id else {"name": name})
        body = await self._call("createPlaylist", post=True, **params)
        return str(_dig(body, "playlist").get("id") or playlist_id)

    async def update_playlist(self, playlist_id: str, name: str) -> None:
        await self._call("updatePlaylist", post=True, playlistId=playlist_id, name=name)

    async def get_starred2(self) -> dict[str, Any]:
        return _dig(await self._call("getStarred2"), "starred2")

    async def get_playlist(self, playlist_id: str) -> dict[str, Any]:
        return _dig(await self._call("getPlaylist", id=playlist_id), "playlist")

    async def get_song(self, song_id: str) -> dict[str, Any]:
        return _dig(await self._call("getSong", id=song_id), "song")

    async def search3(
        self, query: str, song_count: int = 20, music_folder_id: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query": query, "songCount": song_count}
        params.update({"artistCount": 0, "albumCount": 0})
        if music_folder_id:
            params["musicFolderId"] = music_folder_id
        body = await self._call("search3", **params)
        songs: list[dict[str, Any]] = _dig(body, "searchResult3").get("song") or []
        return songs

    async def get_music_folders(self) -> list[dict[str, Any]]:
        body = await self._call("getMusicFolders")
        folders: list[dict[str, Any]] = _dig(body, "musicFolders").get("musicFolder") or []
        return folders
