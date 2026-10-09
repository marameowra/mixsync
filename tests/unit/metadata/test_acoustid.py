import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from mixsync.core.config import Settings
from mixsync.core.errors import PermanentError, TransientError
from mixsync.core.types import Fingerprint
from mixsync.metadata import acoustid
from mixsync.metadata.acoustid import AcoustIdClient, fingerprint
from mixsync.ratelimit.client import PoliteClient

Make = Callable[[str, Callable[[httpx.Request], httpx.Response]], PoliteClient]
FP = Fingerprint(215.6, "AQADtEqUaEmUJAHAH")


class FakeProc:
    def __init__(self, out: bytes, err: bytes, code: int) -> None:
        self._io, self.returncode = (out, err), code

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._io


def fake_fpcalc(monkeypatch: pytest.MonkeyPatch, out: bytes, err: bytes, code: int) -> list[Any]:
    calls: list[Any] = []

    async def fake(*args: Any, **_kw: Any) -> FakeProc:
        calls.append(args)
        return FakeProc(out, err, code)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    return calls


async def test_fingerprint_parses_output(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_fpcalc(monkeypatch, b'{"duration": 215.6, "fingerprint": "AQAD"}', b"", 0)
    assert await fingerprint(Path("a.flac")) == Fingerprint(215.6, "AQAD")
    assert calls == [("fpcalc", "-json", "a.flac")]


@pytest.mark.parametrize(("out", "code"), [(b"", 1), (b"not json", 0), (b'{"duration": 1}', 0)])
async def test_fingerprint_undecodable(monkeypatch: pytest.MonkeyPatch, out: bytes, code: int):
    fake_fpcalc(monkeypatch, out, b"ERROR: decode", code)
    with pytest.raises(PermanentError):
        await fingerprint(Path("junk.bin"))


async def test_fingerprint_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing(*_a: Any, **_k: Any) -> None:
        raise FileNotFoundError("fpcalc")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)
    with pytest.raises(RuntimeError, match="fpcalc"):
        await fingerprint(Path("a.flac"))


@pytest.mark.skipif(shutil.which("fpcalc") is None, reason="fpcalc not installed")
async def test_fingerprint_real_binary(tmp_path: Path) -> None:
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"not audio")
    with pytest.raises(PermanentError):
        await acoustid.fingerprint(junk)


def keyed(settings: Settings, key: str = "appkey") -> Settings:
    return settings.model_copy(update={"acoustid_app_key": key})


async def test_lookup(make_client: Make, settings: Settings) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            text=(Path(__file__).parents[2] / "cassettes" / "acoustid" / "lookup.json").read_text(),
        )

    results = await AcoustIdClient(make_client("acoustid", handler), keyed(settings)).lookup(FP)
    assert [(r.id[:8], r.score, len(r.recording_ids)) for r in results] == [
        ("9ff43b6a", 0.97, 2),
        ("aaaaaaaa", 0.4, 0),
    ]
    assert results[0].recording_ids[0] == "60bd9d53-01ff-4562-8058-eb44b3940317"
    p = seen[0].url.params
    assert (p["client"], p["meta"], p["duration"], p["fingerprint"]) == (
        "appkey",
        "recordings",
        "216",
        FP.fingerprint,
    )
    assert seen[0].headers["user-agent"].startswith("MixSync/")


async def test_lookup_503_is_transient(make_client: Make, settings: Settings) -> None:
    client = AcoustIdClient(
        make_client("acoustid", lambda _r: httpx.Response(503)), keyed(settings)
    )
    with pytest.raises(TransientError):
        await client.lookup(FP)


async def test_lookup_unreachable_is_transient(make_client: Make, settings: Settings) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(TransientError):
        await AcoustIdClient(make_client("acoustid", down), keyed(settings)).lookup(FP)


async def test_lookup_requires_key(make_client: Make, settings: Settings) -> None:
    client = AcoustIdClient(make_client("acoustid", lambda _r: httpx.Response(200)), settings)
    with pytest.raises(RuntimeError, match="ACOUSTID_APP_KEY"):
        await client.lookup(FP)
