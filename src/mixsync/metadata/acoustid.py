# API details (parameter names, gzip POST body, repeated `id` for status) were checked against
# https://acoustid.org/webservice on 2026-10-10.
import asyncio
import gzip
import json
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

import mutagen
from mutagen.flac import FLAC

from mixsync.core.config import Settings
from mixsync.core.errors import PermanentError
from mixsync.core.types import AcoustIdResult, Fingerprint
from mixsync.metadata._http import get_json
from mixsync.metadata.models import AcoustIdResponse, AcoustIdSubmitResponse
from mixsync.ratelimit.client import PoliteClient

_URL = "https://api.acoustid.org/v2/lookup"
_SUBMIT_URL = "https://api.acoustid.org/v2/submit"
_STATUS_URL = "https://api.acoustid.org/v2/submission_status"


async def _fpcalc(*args: str) -> tuple[int | None, bytes, bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "fpcalc",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise RuntimeError("fpcalc (chromaprint) is not installed or not on PATH") from e
    out, err = await proc.communicate()
    return proc.returncode, out, err


def _duration(path: Path) -> float | None:
    try:
        f = cast(Any, mutagen.File(path))  # pyright: ignore[reportUnknownMemberType]
        return float(f.info.length) if f is not None else None
    except Exception:
        return None


def _flac_info(path: Path) -> Any | None:
    try:
        return cast(Any, FLAC(path).info)  # pyright: ignore[reportUnknownMemberType]
    except Exception:
        return None


async def _flac_fully_decodes(path: Path, info: Any) -> bool:
    """Does ffmpeg decode every sample STREAMINFO promises?"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-xerror", "-v", "quiet", "-i", str(path),
            "-map", "0:a", "-f", "s16le", "-",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
    except FileNotFoundError as e:
        raise RuntimeError("ffmpeg is not installed or not on PATH") from e
    assert proc.stdout is not None
    size = 0
    try:
        while chunk := await proc.stdout.read(1 << 20):  # counted, not kept: files can be large
            size += len(chunk)
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    return size == info.total_samples * info.channels * 2


async def fingerprint(path: Path) -> Fingerprint:
    # fpcalc can exit 0 on a FLAC whose last frames are corrupt, so decode it all first.
    info = await asyncio.to_thread(_flac_info, path)
    # shortcut: total_samples == 0 (unknown length) cannot be compared, so it is not checked
    proven = False
    if info is not None and info.total_samples:
        proven = await _flac_fully_decodes(path, info)
        if not proven:
            raise PermanentError(f"{path} does not decode completely")
    code, out, err = await _fpcalc("-json", str(path))
    if code != 0 and proven:
        # Junk after the last frame makes fpcalc exit 3 on intact audio; stopping 1 s early
        # skips it. Safe because the whole stream decoded above.
        duration = await asyncio.to_thread(_duration, path)
        if duration is not None and duration > 1:
            code, out, _ = await _fpcalc("-length", str(int(duration) - 1), "-json", str(path))
    if code != 0:
        raise PermanentError(f"fpcalc failed on {path}: {err.decode(errors='replace').strip()}")
    try:
        data = json.loads(out)
        return Fingerprint(float(data["duration"]), str(data["fingerprint"]))
    except (ValueError, KeyError, TypeError) as e:
        raise PermanentError(f"fpcalc gave unusable output for {path}") from e


class AcoustIdClient:
    def __init__(self, client: PoliteClient, settings: Settings) -> None:
        self._client = client
        self._key = settings.acoustid_app_key
        self._user = settings.acoustid_user_key

    async def lookup(self, fp: Fingerprint) -> list[AcoustIdResult]:
        if not self._key:
            raise RuntimeError("MIXSYNC_ACOUSTID_APP_KEY is not set; AcoustID lookups need it")
        data = await get_json(
            self._client,
            _URL,
            {
                "client": self._key,
                "meta": "recordings",
                "duration": round(fp.duration),
                "fingerprint": fp.fingerprint,
                "format": "json",
            },
        )
        resp = AcoustIdResponse.model_validate(data)
        if resp.status != "ok":
            raise PermanentError(f"AcoustID returned status {resp.status}")
        return [
            AcoustIdResult(h.id, h.score, tuple(r.id for r in h.recordings)) for h in resp.results
        ]

    async def submit(self, items: list[tuple[Fingerprint, str]]) -> list[int]:
        """One batched, gzip-compressed POST; returns the submission ids."""
        if not (self._key and self._user):
            raise RuntimeError("MIXSYNC_ACOUSTID_APP_KEY and _USER_KEY are needed to submit")
        form = {"client": self._key, "user": self._user, "format": "json"}
        for i, (fp, mbid) in enumerate(items):
            form |= {
                f"fingerprint.{i}": fp.fingerprint,
                f"duration.{i}": str(round(fp.duration)),
                f"mbid.{i}": mbid,
            }
        data = await get_json(
            self._client,
            _SUBMIT_URL,
            {},
            method="POST",
            content=gzip.compress(urlencode(form).encode()),
            headers={
                "Content-Encoding": "gzip",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        resp = AcoustIdSubmitResponse.model_validate(data)
        if resp.status != "ok":
            raise PermanentError(f"AcoustID returned status {resp.status}")
        return [s.id for s in resp.submissions]

    async def submission_status(self, ids: list[int]) -> dict[int, str]:
        data = await get_json(
            self._client,
            _STATUS_URL,
            {"client": self._key, "id": ids, "format": "json"},
            headers={"Cache-Control": "no-cache"},  # never answered from the cache
        )
        resp = AcoustIdSubmitResponse.model_validate(data)
        if resp.status != "ok":
            raise PermanentError(f"AcoustID returned status {resp.status}")
        return {s.id: s.status for s in resp.submissions}
