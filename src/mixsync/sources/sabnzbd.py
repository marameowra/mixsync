"""SABnzbd API client for the NZBs Prowlarr finds. Usenet downloads do not seed: once imported
they are released like Soulseek ones (data-safety.md).

Every call is a POST with the API key in the form body, never in the URL, so no request log can
show it. Fields read (SABnzbd's official API docs): `mode=queue` slots `nzo_id`, `filename`,
`cat`, `status`, `mb`, `mbleft` (strings, MB); `mode=history` slots `nzo_id`, `name`,
`category`, `status` (`Completed`, `Failed`, or a post-processing step), `storage` (the final
folder), `bytes`. An aborted job lands in history as `Failed`.
"""

import asyncio
from pathlib import Path
from typing import Any

import httpx

from mixsync.core.errors import AuthError, PermanentError, TransientError
from mixsync.core.matching import Candidate, TransferInfo, TransferStatus
from mixsync.ratelimit.client import PoliteClient

CATEGORY = "mixsync"


def job_name(nzb_hash: str) -> str:
    """The name MixSync gives a job, so a re-run finds it again without storing its nzo_id."""
    return f"mixsync-{nzb_hash}"


class Sabnzbd:
    def __init__(self, client: PoliteClient, base_url: str, api_key: str, complete_dir: Path):
        self._client = client
        self._url = f"{base_url.rstrip('/')}/api"
        self._key = api_key
        self.complete_dir = complete_dir  # the `mixsync` category's folder, same in both containers

    async def _call(self, mode: str, files: Any = None, **params: str) -> dict[str, Any]:
        data = {"mode": mode, "output": "json", "apikey": self._key, **params}
        try:
            response = await self._client.post(self._url, data=data, files=files)
        except httpx.TransportError as e:  # never the request itself: its body has the key
            raise TransientError(f"sabnzbd unreachable ({type(e).__name__})") from e
        if response.status_code >= 500:
            raise TransientError(f"sabnzbd returned {response.status_code}")
        try:
            body: dict[str, Any] = response.json()
        except ValueError:  # older versions answer errors in plain text, "error: ..."
            body = {"status": False, "error": response.text.removeprefix("error:").strip()}
        if response.status_code in (401, 403) or "API Key" in str(body.get("error", "")):
            raise AuthError(
                "sabnzbd rejected the API key; check sabnzbd_api_key on /settings"
                " (or MIXSYNC_SABNZBD_API_KEY)"
            )
        if response.status_code != 200:
            raise PermanentError(f"sabnzbd {mode} returned {response.status_code}")
        return body

    async def _slots(self, mode: str, name: str) -> list[dict[str, Any]]:
        body = await self._call(mode, cat=CATEGORY, search=name)
        slots: list[dict[str, Any]] = body.get(mode, {}).get("slots", [])
        cat = "cat" if mode == "queue" else "category"
        title = "filename" if mode == "queue" else "name"
        # a user's own job is never ours, whatever the search matched
        return [s for s in slots if s.get(cat) == CATEGORY and s.get(title) == name]

    async def job(self, nzb_hash: str) -> tuple[str, dict[str, Any]] | None:
        """("queue" | "history", slot) of MixSync's job for this NZB. The queue is read first:
        a job moving to history in between is then still found there."""
        name = job_name(nzb_hash)
        for mode in ("queue", "history"):
            if slots := await self._slots(mode, name):
                return mode, slots[0]
        return None

    async def add(self, nzb: bytes, nzb_hash: str) -> None:
        if await self.job(nzb_hash) is not None:  # a re-run: already added
            return
        body = await self._call(
            "addfile",
            files={"name": (f"{job_name(nzb_hash)}.nzb", nzb, "application/x-nzb")},
            cat=CATEGORY,
            nzbname=job_name(nzb_hash),
        )
        if not body.get("status") or not body.get("nzo_ids"):
            raise PermanentError(f"sabnzbd refused the NZB: {body.get('error', '')}")

    def _final(self, storage: str) -> Path:
        folder = Path(storage)
        if ".." in folder.parts or self.complete_dir.resolve() not in folder.resolve().parents:
            raise PermanentError(f"sabnzbd finished outside {self.complete_dir}: {storage!r}")
        return folder

    async def status(self, candidate: Candidate) -> TransferInfo:
        """Done means history says Completed; Failed (aborted too) or gone has failed."""
        found = await self.job(candidate.nzb_hash)
        if found is None:
            return TransferInfo(TransferStatus.FAILED, 0)
        mode, slot = found
        if mode == "queue":
            done = int((float(slot.get("mb", 0)) - float(slot.get("mbleft", 0))) * 1_000_000)
            return TransferInfo(TransferStatus.IN_PROGRESS if done else TransferStatus.QUEUED, done)
        done = int(slot.get("bytes", 0))
        if slot.get("status") == "Failed":
            return TransferInfo(TransferStatus.FAILED, done)
        if slot.get("status") != "Completed":  # verifying, repairing, extracting, moving
            return TransferInfo(TransferStatus.IN_PROGRESS, done)
        folder = self._final(str(slot.get("storage", "")))
        names = [f.path.rpartition("\\")[2] for f in candidate.files]
        if any(n in ("", ".", "..") or "/" in n for n in names):
            raise PermanentError("unsafe file name in the NZB")
        files = await asyncio.to_thread(lambda: [p for p in folder.rglob("*") if p.is_file()])
        paths: list[str] = []
        for n in names:
            hits = [p for p in files if p.name == n]
            if folder / n in hits or not hits:  # top level first; a missing file fails the check
                paths.append(str(folder / n))
            elif len(hits) == 1:  # SABnzbd may unpack into a subfolder
                paths.append(str(hits[0]))
            else:
                raise PermanentError(f"{n!r} is in more than one subfolder of the job")
        return TransferInfo(TransferStatus.DONE, done, tuple(paths))

    async def remove(self, nzb_hash: str) -> None:
        """Delete MixSync's job. Partial files go with a queued or failed one; a completed job's
        folder is never deleted by SABnzbd (the pipeline releases its files after import)."""
        found = await self.job(nzb_hash)
        if found is None:
            return
        mode, slot = found
        params = {"name": "delete", "value": str(slot["nzo_id"])}
        if mode == "history":
            params["archive"] = "0"
        if mode == "queue" or slot.get("status") == "Failed":
            params["del_files"] = "1"
        await self._call(mode, **params)
