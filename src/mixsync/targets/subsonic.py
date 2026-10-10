import hashlib
import secrets
from dataclasses import dataclass
from typing import Any

import httpx

from mixsync.core.errors import PermanentError, TransientError
from mixsync.ratelimit.client import PoliteClient

_AUTH_CODES = {40, 41}
_NOT_AUTHORIZED = 50


@dataclass(frozen=True)
class ScanStatus:
    scanning: bool
    count: int | None


class SubsonicClient:
    def __init__(self, client: PoliteClient, base_url: str, user: str, password: str) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._user = user
        self._password = password

    def _auth(self) -> dict[str, str]:
        salt = secrets.token_hex(8)
        token = hashlib.md5((self._password + salt).encode()).hexdigest()  # noqa: S324 - Subsonic protocol
        return {"u": self._user, "t": token, "s": salt, "v": "1.16.1", "c": "mixsync", "f": "json"}

    async def _call(self, endpoint: str, **params: str) -> dict[str, Any]:
        url = f"{self._base}/rest/{endpoint}"
        try:
            response = await self._client.get(url, params={**self._auth(), **params})
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
                raise PermanentError(
                    f"Navidrome rejected the credentials ({message}); "
                    "check MIXSYNC_NAVIDROME_USER and its password"
                )
            if code == _NOT_AUTHORIZED:
                raise PermanentError(
                    f"Navidrome user is not authorized for {endpoint} ({message}); "
                    "MIXSYNC_NAVIDROME_USER must be an admin user to start scans"
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
