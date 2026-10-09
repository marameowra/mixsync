import asyncio
import json
from pathlib import Path

from mixsync.core.config import Settings
from mixsync.core.errors import PermanentError
from mixsync.core.types import AcoustIdResult, Fingerprint
from mixsync.metadata._http import get_json
from mixsync.metadata.models import AcoustIdResponse
from mixsync.ratelimit.client import PoliteClient

_URL = "https://api.acoustid.org/v2/lookup"


async def fingerprint(path: Path) -> Fingerprint:
    try:
        proc = await asyncio.create_subprocess_exec(
            "fpcalc",
            "-json",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise RuntimeError("fpcalc (chromaprint) is not installed or not on PATH") from e
    out, err = await proc.communicate()
    if proc.returncode != 0:
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
