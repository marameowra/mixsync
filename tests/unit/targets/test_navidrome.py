import hashlib
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from mixsync.core.errors import PermanentError, TransientError
from mixsync.ratelimit.client import PoliteClient
from mixsync.targets import subsonic
from mixsync.targets.navidrome import NavidromeTarget
from mixsync.targets.subsonic import ScanStatus, SubsonicClient

Make = Callable[[str, Callable[[httpx.Request], httpx.Response]], PoliteClient]
FIXTURES = Path(__file__).parents[2] / "fixtures" / "navidrome"
PASSWORD = "hunter2-secret"
SALT = "0123456789abcdef"


def fixture(name: str) -> httpx.Response:
    return httpx.Response(200, content=(FIXTURES / f"{name}.json").read_bytes())


def subsonic_client(
    make: Make, handler: Callable[[httpx.Request], httpx.Response]
) -> SubsonicClient:
    return SubsonicClient(make("navidrome", handler), "http://nd:4533/", "admin", PASSWORD)


@pytest.fixture(autouse=True)
def fixed_salt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subsonic.secrets, "token_hex", lambda _n: SALT)


async def test_rescan_sends_token_auth_and_never_the_password(make_client: Make) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return fixture("ok")

    await NavidromeTarget(
        make_client("navidrome", handler), "http://nd:4533", "admin", PASSWORD
    ).rescan()

    (request,) = seen
    assert request.url.path == "/rest/startScan"
    params = dict(request.url.params)
    assert params == {
        "u": "admin",
        "t": hashlib.md5((PASSWORD + SALT).encode()).hexdigest(),
        "s": SALT,
        "v": "1.16.1",
        "c": "mixsync",
        "f": "json",
        "fullScan": "false",
    }
    assert PASSWORD not in str(request.url)


async def test_full_scan_and_ping(make_client: Make) -> None:
    paths: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append((request.url.path, request.url.params.get("fullScan")))
        return fixture("ok")

    client = subsonic_client(make_client, handler)
    await client.start_scan(full=True)
    await client.ping()
    assert paths == [("/rest/startScan", "true"), ("/rest/ping", None)]


async def test_scan_status(make_client: Make) -> None:
    client = subsonic_client(make_client, lambda _r: fixture("scan_status"))
    assert await client.get_scan_status() == ScanStatus(True, 42)


async def test_scan_status_missing_count(make_client: Make) -> None:
    body = b'{"subsonic-response": {"status": "ok", "scanStatus": {"scanning": false}}}'
    client = subsonic_client(make_client, lambda _r: httpx.Response(200, content=body))
    assert await client.get_scan_status() == ScanStatus(False, None)


@pytest.mark.parametrize(
    ("name", "needle"),
    [("error_40", "check MIXSYNC_NAVIDROME_USER"), ("error_50", "must be an admin")],
)
async def test_failed_status_is_permanent(make_client: Make, name: str, needle: str) -> None:
    client = subsonic_client(make_client, lambda _r: fixture(name))
    with pytest.raises(PermanentError, match="MIXSYNC_NAVIDROME_USER") as exc:
        await client.start_scan()
    assert needle in str(exc.value)
    assert PASSWORD not in str(exc.value)


async def test_5xx_is_transient(make_client: Make) -> None:
    client = subsonic_client(make_client, lambda _r: httpx.Response(503))
    with pytest.raises(TransientError):
        await client.ping()


async def test_transport_error_is_transient_and_leaks_nothing(make_client: Make) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {request.url}", request=request)

    client = subsonic_client(make_client, handler)
    with pytest.raises(TransientError) as exc:
        await client.ping()
    assert PASSWORD not in str(exc.value)
    assert SALT not in str(exc.value)
    assert exc.value.__cause__ is None
