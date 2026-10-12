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


# --- per-user client ---

from mixsync.core.errors import AuthError  # noqa: E402
from mixsync.targets.navidrome import NavidromeUser  # noqa: E402


def ok(**payload: object) -> httpx.Response:
    return httpx.Response(
        200, json={"subsonic-response": {"status": "ok", "version": "1.16.1", **payload}}
    )


def err(code: int) -> httpx.Response:
    body = {"status": "failed", "error": {"code": code, "message": "nope"}}
    return httpx.Response(200, json={"subsonic-response": body})


def user(make: Make, handler: Callable[[httpx.Request], httpx.Response]) -> NavidromeUser:
    return NavidromeUser.for_user(make("navidrome", handler), "http://nd:4533", "ann", PASSWORD)


async def test_starred_and_play_counts(make_client: Make) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/getStarred2":
            return ok(starred2={"song": [{"id": "s1"}, {"id": "s2"}]})
        song_id = request.url.params["id"]
        return ok(song={"id": song_id, **({"playCount": 3} if song_id == "s1" else {})})

    u = user(make_client, handler)
    assert await u.starred_song_ids() == {"s1", "s2"}
    assert await u.play_counts(["s1", "s2"]) == {"s1": 3, "s2": 0}


async def test_resolve_picks_exact_path_among_hits(make_client: Make) -> None:
    songs = [
        {"id": "a", "path": "/data/discover/A/x.flac"},
        {"id": "b", "path": "/data/library/A/x.flac"},
    ]
    u = user(make_client, lambda r: ok(searchResult3={"song": songs}))
    assert await u.resolve("/data/library/A/x.flac", "x", "A") == "b"
    assert await u.resolve("/data/other.flac", "x", "A") is None


async def test_search3_and_music_folders_parse(make_client: Make) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/getMusicFolders":
            return ok(musicFolders={"musicFolder": [{"id": 1, "name": "music"}]})
        assert request.url.params["musicFolderId"] == "2"
        return ok(searchResult3={"song": [{"id": "a"}]})

    sc = SubsonicClient(make_client("navidrome", handler), "http://nd", "ann", PASSWORD)
    assert await sc.get_music_folders() == [{"id": 1, "name": "music"}]
    assert await sc.search3("q", music_folder_id="2") == [{"id": "a"}]


async def test_push_playlist_creates_then_replaces_then_recreates_on_70(
    make_client: Make,
) -> None:
    seen: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params
        seen.append({k: q.get_list(k) for k in ("playlistId", "name", "songId")})
        if q.get("playlistId") == "gone":
            return err(70)
        return ok(playlist={"id": q.get("playlistId") or "new"})

    u = user(make_client, handler)
    assert await u.push_playlist("Mix", ["a", "b"], None) == "new"
    assert seen[-1] == {"playlistId": [], "name": ["Mix"], "songId": ["a", "b"]}
    assert await u.push_playlist("Mix", ["a"], "p1") == "p1"
    assert seen[-1]["playlistId"] == ["p1"]
    assert await u.push_playlist("Mix", ["a"], "gone") == "new"
    assert seen[-1]["name"] == ["Mix"]


@pytest.mark.parametrize("code", [40, 41])
async def test_auth_errors_name_linked_account_and_hide_secrets(
    make_client: Make, code: int
) -> None:
    u = user(make_client, lambda r: err(code))
    with pytest.raises(AuthError) as exc:
        await u.starred_song_ids()
    message = str(exc.value)
    assert "linked Navidrome account" in message
    assert "MIXSYNC_NAVIDROME" not in message
    assert PASSWORD not in message
    assert hashlib.md5((PASSWORD + SALT).encode()).hexdigest() not in message  # noqa: S324


async def test_transport_error_hides_token(make_client: Make) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(TransientError) as exc:
        await user(make_client, handler).starred_song_ids()
    assert PASSWORD not in str(exc.value)
