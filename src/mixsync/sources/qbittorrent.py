"""qBittorrent Web API (v2) client for the torrents Prowlarr finds. Its files are seeding
downloads (ADR 0022): MixSync copies from them and never removes a finished torrent; only an
unfinished one it dropped goes, with its partial files.

Fields of `GET /api/v2/torrents/info` (qBittorrent's WebUI API docs) this module reads: `hash`,
`progress` (0 to 1), `state` (`error` / `missingFiles` mean the transfer failed), `completed`
(bytes downloaded and verified), `amount_left`. Of `GET /api/v2/torrents/files`: `index`, `name`
(the path under the save path, `/`-separated), `priority` (0: not downloaded).

A pack folder (ADR 0026) is a partial download: only its files are selected, by file priority.
"""

import asyncio
from collections.abc import Collection
from pathlib import Path
from typing import Any

import httpx

from mixsync.core.errors import AuthError, PermanentError, TransientError
from mixsync.core.matching import Candidate, TransferInfo, TransferStatus
from mixsync.ratelimit.client import PoliteClient

CATEGORY = "mixsync"
FILES_TRIES, FILES_WAIT = 10, 0.5  # a just-added torrent's file list: up to 5 s
FAILED_STATES = frozenset({"error", "missingFiles"})
# Only these may be deleted. Never by progress alone: a recheck (checkingUP,
# checkingResumeData) restarts a finished torrent's progress from 0.
DOWNLOADING_STATES = frozenset(
    {"downloading", "stalledDL", "queuedDL", "pausedDL", "stoppedDL", "metaDL", "forcedDL", "error"}
)


def unfinished(t: dict[str, Any] | None) -> bool:
    return (
        t is not None and t.get("state") in DOWNLOADING_STATES and int(t.get("amount_left", 0)) > 0
    )


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
                "qbittorrent rejected the login; check qbittorrent_username and _password"
                " on /settings (or MIXSYNC_QBITTORRENT_USERNAME and _PASSWORD)"
            )

    async def ping(self) -> None:
        """Log in, then read the app version (the settings page's Test button); changes nothing."""
        await self._sign_in()
        response = await self._call("GET", "app/version")
        if response.status_code != 200:
            raise PermanentError(f"qbittorrent app/version returned {response.status_code}")

    async def info(self, infohash: str) -> dict[str, Any] | None:
        """The torrent, if it is in MixSync's category (a user's own torrent is never ours)."""
        response = await self._call(
            "GET", "torrents/info", params={"hashes": infohash, "category": CATEGORY}
        )
        if response.status_code != 200:
            raise PermanentError(f"qbittorrent torrents/info returned {response.status_code}")
        rows: list[dict[str, Any]] = response.json()
        return next((r for r in rows if str(r.get("hash", "")).lower() == infohash), None)

    async def add(self, torrent: bytes, infohash: str, only: Collection[str] | None = None) -> None:
        """Add the torrent whole. With `only` (a pack folder's candidate paths): stopped, every
        other file set to priority 0, then started. A pack already there (shared with another
        request) gets `only` added to its selection; no selected file is ever deselected."""
        ours = False  # this call's add was accepted: only then may a failed selection delete it
        if only is None or await self.info(infohash) is None:
            stopped = {"stopped": "true", "paused": "true"} if only is not None else {}
            response = await self._call(
                "POST",
                "torrents/add",
                files={"torrents": ("release.torrent", torrent, "application/x-bittorrent")},
                data={
                    "category": CATEGORY,
                    "savepath": str(self.save_dir),
                    "autoTMM": "false",  # the category's own rules must not move it elsewhere
                    "contentLayout": "Original",  # files at <save dir>/<torrent name>/, as offered
                    **stopped,  # 5.x reads `stopped`, 4.6 `paused`; each ignores the other
                },
            )
            ours = response.status_code == 200 and response.text.strip() == "Ok."
            if not ours and await self.info(infohash) is None:  # "Fails." answers a re-run's add
                raise PermanentError(f"qbittorrent refused the torrent ({response.status_code})")
        if only is None:
            return
        try:
            await self._select(infohash, set(only))
        except (PermanentError, TransientError) as e:
            if ours:  # stopped since this add: nothing was downloaded
                await self._call(
                    "POST", "torrents/delete", data={"hashes": infohash, "deleteFiles": "true"}
                )
            raise PermanentError(f"qbittorrent could not select the pack folder: {e}") from e

    async def _files(self, infohash: str) -> dict[str, tuple[int, int]]:
        """Each file's candidate path (backslash-joined) -> (index, priority). A torrent just
        added can take a moment to show up."""
        for _ in range(FILES_TRIES):
            response = await self._call("GET", "torrents/files", params={"hash": infohash})
            if response.status_code == 200 and (rows := response.json()):
                return {
                    str(f["name"]).replace("/", "\\"): (int(f["index"]), int(f["priority"]))
                    for f in rows
                }
            await asyncio.sleep(FILES_WAIT)
        raise PermanentError("qbittorrent has no file list for the torrent")

    async def _priority(self, infohash: str, ids: list[int], priority: int) -> None:
        response = await self._call(
            "POST",
            "torrents/filePrio",
            data={"hash": infohash, "id": "|".join(map(str, ids)), "priority": str(priority)},
        )
        if response.status_code != 200:
            raise PermanentError(f"qbittorrent torrents/filePrio returned {response.status_code}")

    async def _select(self, infohash: str, only: set[str]) -> None:
        # shortcut: read-modify-write of priorities; two workers selecting folders of one new
        # pack at once can zero each other's files. Unsafe with multiple workers; lock per
        # infohash if more than one worker runs downloads.
        files = await self._files(infohash)
        if not only <= files.keys():
            raise PermanentError("the pack folder's files are not in the torrent")
        changed = False
        # every file wanted: added just now, or by an add cut short before its selection ran
        if all(p for _, p in files.values()):
            await self._priority(infohash, [i for n, (i, _) in files.items() if n not in only], 0)
            changed = True
        if more := sorted(i for n, (i, p) in files.items() if n in only and not p):
            await self._priority(infohash, more, 1)
            changed = True
        if changed:
            start = await self._call("POST", "torrents/start", data={"hashes": infohash})
            if start.status_code == 404:  # qBittorrent < 5 calls it resume
                start = await self._call("POST", "torrents/resume", data={"hashes": infohash})
            if start.status_code != 200:
                raise PermanentError(f"qbittorrent start returned {start.status_code}")

    async def deselect(self, infohash: str, paths: Collection[str]) -> None:
        """Stop downloading these files of a pack while it is unfinished; the caller leaves out
        any file another live request needs. A torrent added whole is never touched."""
        if not paths or not unfinished(await self.info(infohash)):
            return
        files = await self._files(infohash)
        if all(p for _, p in files.values()):  # added whole: not a pack selection
            return
        if ids := sorted(i for n, (i, p) in files.items() if p and n in paths):
            await self._priority(infohash, ids, 0)

    async def drop_folder(self, infohash: str, paths: Collection[str]) -> None:
        """A dropped pack folder no live request shares. The torrent goes (with its partial
        files) only if these files are its whole selection; if any other file is selected, a
        folder an earlier request imported and seeds, only these files are deselected."""
        if not unfinished(await self.info(infohash)):
            return
        files = await self._files(infohash)
        whole = all(p for _, p in files.values())  # an add cut short before its selection
        if whole or not any(p for n, (_, p) in files.items() if n not in paths):
            await self.remove_unfinished(infohash)
        else:
            await self.deselect(infohash, paths)

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
        """Remove a dropped torrent with its partial files, only while it is downloading. A
        finished or rechecking one is left to qBittorrent's own ratio rules (ADR 0022)."""
        if not unfinished(await self.info(infohash)):
            return
        # Stop it first so it can't finish between the check and the delete.
        stop = await self._call("POST", "torrents/stop", data={"hashes": infohash})
        if stop.status_code == 404:  # qBittorrent < 5 calls it pause
            await self._call("POST", "torrents/pause", data={"hashes": infohash})
        if unfinished(await self.info(infohash)):
            await self._call(
                "POST", "torrents/delete", data={"hashes": infohash, "deleteFiles": "true"}
            )
