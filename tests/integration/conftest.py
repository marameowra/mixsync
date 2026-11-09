import hashlib
import re
import secrets
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

WEB = "http://mixsync-web:8080"
NAVIDROME = "http://navidrome:4533"
LIBRARY = Path("/data/library")
USER, PASSWORD = "admin", "integration"  # mixsync-user in compose.yml
ND_USER, ND_PASSWORD = "admin", "integration"  # MIXSYNC_NAVIDROME_* in compose.yml


@pytest.fixture(autouse=True)
def _block_sockets() -> None:  # pyright: ignore[reportUnusedFunction]
    """Overrides tests/conftest.py: this suite talks to the stack. The compose network is
    internal, so nothing past it is reachable anyway."""


def wait_for[T](what: str, check: Callable[[], T | None], timeout: float = 300) -> T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (found := check()) is not None:
            return found
        time.sleep(2)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}")


def csrf(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match, "no CSRF token on the page"
    return match.group(1)


@pytest.fixture(scope="session")
def web() -> Iterator[httpx.Client]:
    """A logged-in mixsync session."""
    with httpx.Client(base_url=WEB, timeout=30) as client:
        r = client.post("/login", data={"username": USER, "password": PASSWORD})
        assert r.status_code == 303, r.text
        assert "mixsync_session" in client.cookies
        yield client


def request_album(web: httpx.Client, mbid: str) -> None:
    page = web.get("/requests")
    assert page.status_code == 200
    r = web.post("/requests", data={"release": mbid, "csrf_token": csrf(page.text)})
    assert r.status_code == 303, r.text


@pytest.fixture(scope="session")
def navidrome() -> Callable[..., dict[str, Any]]:
    """Subsonic API calls as Navidrome's admin, created on first use (the account mixsync's
    MIXSYNC_NAVIDROME_USER names)."""

    def ready() -> bool | None:
        try:
            return httpx.get(f"{NAVIDROME}/ping", timeout=5).status_code == 200 or None
        except httpx.TransportError:
            return None

    wait_for("Navidrome", ready, 120)
    r = httpx.post(
        f"{NAVIDROME}/auth/createAdmin", json={"username": ND_USER, "password": ND_PASSWORD}
    )
    assert r.status_code == 200, r.text

    def call(endpoint: str, **params: Any) -> dict[str, Any]:
        salt = secrets.token_hex(8)
        token = hashlib.md5((ND_PASSWORD + salt).encode(), usedforsecurity=False).hexdigest()
        auth = {"u": ND_USER, "t": token, "s": salt, "v": "1.16.1", "c": "it", "f": "json"}
        body = httpx.get(f"{NAVIDROME}/rest/{endpoint}", params={**auth, **params}).json()
        assert body["subsonic-response"]["status"] == "ok", body
        return body["subsonic-response"]

    return call
