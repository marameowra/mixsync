"""qBittorrent Web API (v2) client for the torrents Prowlarr finds. Its files are seeding
downloads (ADR 0022): MixSync copies from them and never removes a finished torrent; only an
unfinished one it dropped goes, with its partial files.

Fields of `GET /api/v2/torrents/info` (qBittorrent's WebUI API docs) this module reads: `hash`,
`progress` (0 to 1), `state` (`error` / `missingFiles` mean the transfer failed), `completed`
(bytes downloaded and verified).
"""

from pathlib import Path
from typing import Any

import httpx

from mixsync.core.errors import AuthError, PermanentError, TransientError
from mixsync.core.matching import Candidate, TransferInfo, TransferStatus
from mixsync.ratelimit.client import PoliteClient

CATEGORY = "mixsync"
FAILED_STATES = frozenset({"error", "missingFiles"})


class Qbittorrent:
    def __init__(
        self, client: PoliteClient, base_url: str, username: str, password: str, save_dir: Path
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._login = {"username": username, "password": password}
        self.save_dir = save_dir  # the same path in the qBittorrent and MixSync containers

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._client.request(method, f"{self._base}/api/v2/{path}", **kwargs)
        except httpx.TransportError as e:
            raise TransientError(f"qbittorrent unreachable: {e!r}") from e

    async def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One API call; a 403 (no or expired session cookie) logs in and tries once more."""
        response = await self._send(method, path, **kwargs)
        if response.status_code == 403:
            await self._sign_in()
            response = await self._send(method, path, **kwargs)
        if response.status_code in (401, 403):
            raise AuthError("qbittorrent refused the session")
        if response.status_code >= 500:
            raise TransientError(f"qbittorrent returned {response.status_code}")
        return response

    async def _sign_in(self) -> None:
        """The SID cookie lands in the client's cookie jar; later calls send it."""
        response = await self._send("POST", "auth/login", data=self._login)
        if response.status_code != 200 or response.text.strip() != "Ok.":  # "Fails." / 403 ban
            raise AuthError(
                "qbittorrent rejected the login; check MIXSYNC_QBITTORRENT_USERNAME and _PASSWORD"
            )

    async def info(self, infohash: str) -> dict[str, Any] | None:
        """The torrent, if it is in MixSync's category (a user's own torrent is never ours)."""
        response = await self._call(
            "GET", "torrents/info", params={"hashes": infohash, "category": CATEGORY}
        )
        if response.status_code != 200:
            raise PermanentError(f"qbittorrent torrents/info returned {response.status_code}")
        rows: list[dict[str, Any]] = response.json()
        return next((r for r in rows if str(r.get("hash", "")).lower() == infohash), None)

    async def add(self, torrent: bytes, infohash: str) -> None:
        response = await self._call(
            "POST",
            "torrents/add",
            files={"torrents": ("release.torrent", torrent, "application/x-bittorrent")},
            data={
                "category": CATEGORY,
                "savepath": str(self.save_dir),
                "autoTMM": "false",  # the category's own rules must not move it elsewhere
                "contentLayout": "Original",  # files at <save dir>/<torrent name>/, as offered
            },
        )
        if response.status_code == 200 and response.text.strip() == "Ok.":
            return
        if await self.info(infohash) is None:  # "Fails." also answers a re-run's second add
            raise PermanentError(f"qbittorrent refused the torrent ({response.status_code})")

    def local(self, path: str) -> Path:
        """Where a candidate file (backslash-joined torrent path) lands. Torrent paths are
        untrusted: anything that could step out of the save dir is refused."""
        parts = path.split("\\")
        if any(p in ("", ".", "..") or "/" in p for p in parts):
            raise PermanentError(f"unsafe path in torrent: {path!r}")
        return self.save_dir.joinpath(*parts)

    async def status(self, candidate: Candidate) -> TransferInfo:
        """Done means 100 %; a torrent that vanished or errored has failed."""
        t = await self.info(candidate.infohash)
        done = int(t.get("completed", 0)) if t else 0
        if t is None or t.get("state") in FAILED_STATES:
            return TransferInfo(TransferStatus.FAILED, done)
        if float(t.get("progress", 0)) < 1:
            return TransferInfo(TransferStatus.IN_PROGRESS if done else TransferStatus.QUEUED, done)
        paths = tuple(str(self.local(f.path)) for f in candidate.files)
        return TransferInfo(TransferStatus.DONE, done, paths)

    async def remove_unfinished(self, infohash: str) -> None:
        """Remove a dropped torrent with its partial files, only while it is unfinished. A
        finished one is left to qBittorrent's own ratio rules (ADR 0022)."""
        # shortcut: a torrent that finishes between the check and the delete is removed; its
        # files were never imported. Fence it with a qBittorrent tag if that ever matters.
        t = await self.info(infohash)
        if t is not None and float(t.get("progress", 0)) < 1:
            await self._call(
                "POST", "torrents/delete", data={"hashes": infohash, "deleteFiles": "true"}
            )
