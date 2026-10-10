import asyncio
import re
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from mixsync.core.errors import PermanentError, TransientError
from mixsync.core.matching import Candidate, CandidateFile, TransferInfo, TransferStatus
from mixsync.ratelimit.client import PoliteClient

AUDIO = frozenset({"mp3", "flac", "ogg", "opus", "m4a", "aac", "wav", "wma", "ape", "wv", "aiff"})
RESPONSE_TRIES = 10
_DISC_DIR = re.compile(r"^(?:cd|dis[ck])[\s._-]*\d+", re.IGNORECASE)


def _split(path: str) -> tuple[str, str]:
    """(album folder, file name). Disc subfolders belong to the album folder above them."""
    folder, _, name = path.rpartition("\\")
    head, _, last = folder.rpartition("\\")
    return (head if _DISC_DIR.match(last) else folder), name


def map_responses(responses: list[dict[str, Any]]) -> list[Candidate]:
    """One Candidate per peer folder. Non-audio files are dropped; a `.cue` is remembered."""
    out: list[Candidate] = []
    for r in responses:
        folders: dict[str, list[CandidateFile]] = defaultdict(list)
        cues: set[str] = set()
        for f in r["files"]:
            folder, name = _split(f["filename"])
            ext = name.rpartition(".")[2].lower() if "." in name else ""
            if ext == "cue":
                cues.add(folder)
            elif ext in AUDIO:
                folders[folder].append(
                    CandidateFile(
                        path=f["filename"],
                        size=f["size"],
                        extension=ext,
                        bitrate=f.get("bitRate"),
                        duration=f.get("length"),
                        bit_depth=f.get("bitDepth"),
                        sample_rate=f.get("sampleRate"),
                    )
                )
        for folder, files in folders.items():
            out.append(
                Candidate(
                    username=r["username"],
                    folder=folder,
                    files=tuple(files),
                    has_free_slot=r["hasFreeUploadSlot"],
                    queue_length=r["queueLength"],
                    upload_speed=r["uploadSpeed"],
                    has_cue=folder in cues,
                )
            )
    return out


def _state(slskd_state: str) -> TransferStatus:
    if "Succeeded" in slskd_state:
        return TransferStatus.DONE
    if "Completed" in slskd_state:
        return TransferStatus.FAILED
    if "InProgress" in slskd_state or "Initializing" in slskd_state:
        return TransferStatus.IN_PROGRESS
    return TransferStatus.QUEUED


class SlskdSource:
    def __init__(
        self,
        client: PoliteClient,
        base_url: str,
        api_key: str,
        *,
        downloads_dir: Path,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_interval: float = 1.0,
        max_polls: int = 60,
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/") + "/api/v0"
        self._headers = {"X-API-Key": api_key}
        self._downloads = downloads_dir
        self._sleep = sleep
        self._poll_interval = poll_interval
        self._max_polls = max_polls

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(
                method, self._base + path, headers=self._headers, **kwargs
            )
        except httpx.TransportError as e:
            raise TransientError(f"slskd unreachable: {e!r}") from e
        code = response.status_code
        if code in (401, 403):
            raise PermanentError(
                f"slskd rejected the API key ({code}); check MIXSYNC_SLSKD_API_KEY"
            )
        if code >= 500:
            raise TransientError(f"slskd {path} returned {code}")
        if code >= 400:
            raise PermanentError(f"slskd {path} returned {code}")
        return response.json() if response.content else None

    async def search(self, query: str) -> list[Candidate]:
        search_id = str(uuid.uuid4())
        await self._call("POST", "/searches", json={"id": search_id, "searchText": query})
        for _ in range(self._max_polls):
            state = await self._call("GET", f"/searches/{search_id}")
            if state["isComplete"]:
                return await self._responses(search_id, state["responseCount"])
            await self._sleep(self._poll_interval)
        raise TransientError(f"slskd search {query!r} did not complete")

    async def _responses(self, search_id: str, expected: int) -> list[Candidate]:
        # slskd persists responses after reporting completion; early reads can be empty.
        for _ in range(RESPONSE_TRIES):
            data = await self._call("GET", f"/searches/{search_id}/responses")
            if data or not expected:
                return map_responses(data)
            await self._sleep(self._poll_interval)
        raise TransientError(f"slskd search {search_id} reports {expected} responses but gave none")

    async def enqueue(self, candidate: Candidate) -> None:
        body = [{"filename": f.path, "size": f.size} for f in candidate.files]
        result = await self._call("POST", f"/transfers/downloads/{candidate.username}", json=body)
        if result and result.get("failed"):
            raise PermanentError(f"slskd would not enqueue from {candidate.username}: {result}")

    async def status(self, candidate: Candidate) -> TransferInfo:
        data = await self._call("GET", f"/transfers/downloads/{candidate.username}")
        wanted = {f.path for f in candidate.files}
        mine = [t for d in data["directories"] for t in d["files"] if t["filename"] in wanted]
        sent = sum(t["bytesTransferred"] for t in mine)
        states = {_state(t["state"]) for t in mine}
        if len(mine) < len(wanted):  # slskd has not listed every file yet
            return TransferInfo(TransferStatus.QUEUED, sent)
        for s in (TransferStatus.FAILED, TransferStatus.IN_PROGRESS, TransferStatus.QUEUED):
            if s in states:
                return TransferInfo(s, sent)
        return TransferInfo(
            TransferStatus.DONE, sent, tuple(self._local(f) for f in candidate.files)
        )

    def _local(self, f: CandidateFile) -> str:
        """slskd saves to <downloads>/<the file's own parent folder name>/<file name> (default
        `Transfers.Download.Destination.Subdirectory` = ${SOURCE_DIRECTORY}; see
        DownloadService.DeriveDestination). A disc subfolder is therefore its own directory.
        Names slskd had to sanitize or de-duplicate will not exist here; callers check."""
        parts = f.path.split("\\")
        return str(self._downloads.joinpath(*parts[-2:]))
