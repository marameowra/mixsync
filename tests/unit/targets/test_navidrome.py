import hashlib
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from mixsync.core.errors import PermanentError, TransientError
from mixsync.ratelimit.client import PoliteClient
from mixsync.targets import subsonic
from mixsync.targets.navidrome import NavidromeTarget
from mixsync.targets.subsonic import SubsonicClient

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
from mixsync.targets.subsonic import NotAuthorizedError  # noqa: E402


def ok(**payload: object) -> httpx.Response:
    return httpx.Response(
        200, json={"subsonic-response": {"status": "ok", "version": "1.16.1", **payload}}
    )


def err(code: int) -> httpx.Response:
    body = {"status": "failed", "error": {"code": code, "message": "nope"}}
    return httpx.Response(200, json={"subsonic-response": body})


def user(make: Make, handler: Callable[[httpx.Request], httpx.Response]) -> NavidromeUser:
    return NavidromeUser(make("navidrome", handler), "http://nd:4533", "ann", PASSWORD)


async def test_starred_and_play_counts(make_client: Make) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/getStarred2":
            return ok(starred2={"song": [{"id": "s1"}, {"id": "s2"}]})
        song_id = request.url.params["id"]
        return ok(song={"id": song_id, **({"playCount": 3} if song_id == "s1" else {})})

    u = user(make_client, handler)
    assert await u.starred_song_ids() == {"s1", "s2"}
    assert await u.play_counts(["s1", "s2"]) == {"s1": 3, "s2": 0}


async def test_resolve_picks_recording_mbid_among_hits(make_client: Make) -> None:
    songs = [
        {"id": "a", "musicBrainzId": "mbid-1"},
        {"id": "b", "musicBrainzId": "mbid-2"},
        {"id": "c"},
    ]
    u = user(make_client, lambda r: ok(searchResult3={"song": songs}))
    assert await u.resolve("mbid-2", "x", "A") == "b"
    assert await u.resolve("mbid-9", "x", "A") is None


async def test_resolve_passes_music_folder_and_folder_lookup(make_client: Make) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/getMusicFolders":
            folders = [{"id": 1, "name": "music"}, {"id": 2, "name": "discover"}]
            return ok(musicFolders={"musicFolder": folders})
        assert request.url.params["musicFolderId"] == "2"
        assert request.url.params["query"] == "A x"
        return ok(searchResult3={"song": [{"id": "a", "musicBrainzId": "m"}]})

    u = user(make_client, handler)
    folder = await u.music_folder_id("discover")
    assert folder == "2"
    assert await u.music_folder_id("nope") is None
    assert await u.resolve("m", "x", "A", folder) == "a"


async def test_search3_and_music_folders_parse(make_client: Make) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/getMusicFolders":
            return ok(musicFolders={"musicFolder": [{"id": 1, "name": "music"}]})
        assert request.url.params["musicFolderId"] == "2"
        return ok(searchResult3={"song": [{"id": "a"}]})

    sc = SubsonicClient(make_client("navidrome", handler), "http://nd", "ann", PASSWORD)
    assert await sc.get_music_folders() == [{"id": 1, "name": "music"}]
    assert await sc.search3("q", music_folder_id="2") == [{"id": "a"}]


def form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode())


@pytest.mark.parametrize("gone", [70, 50])
async def test_push_playlist_posts_form_and_recreates_when_gone(
    make_client: Make, gone: int
) -> None:
    seen: list[tuple[str, str, dict[str, list[str]]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        f = form(request)
        seen.append((request.method, request.url.path, f))
        assert not request.url.query  # nothing, token included, in the URL
        assert f["u"] == ["ann"]
        assert f["s"] == [SALT]
        assert f["t"] == [hashlib.md5((PASSWORD + SALT).encode()).hexdigest()]  # noqa: S324
        assert (f["v"], f["c"], f["f"]) == (["1.16.1"], ["mixsync"], ["json"])
        if f.get("playlistId") == ["gone"]:
            return err(gone)
        return ok(playlist={"id": f.get("playlistId", ["new"])[0]})

    u = user(make_client, handler)
    assert await u.push_playlist("Mix", ["a", "b"], None) == "new"
    method, path, f = seen[-1]
    assert (method, path) == ("POST", "/rest/createPlaylist")
    assert f["songId"] == ["a", "b"]
    assert f["name"] == ["Mix"]
    assert "playlistId" not in f

    seen.clear()
    assert await u.push_playlist("Renamed", ["a"], "p1") == "p1"
    assert [(p, f.get("name"), f.get("playlistId")) for _, p, f in seen] == [
        ("/rest/createPlaylist", None, ["p1"]),
        ("/rest/updatePlaylist", ["Renamed"], ["p1"]),
    ]

    seen.clear()
    assert await u.push_playlist("Mix", ["a"], "gone") == "new"
    assert seen[-1][2]["name"] == ["Mix"]


async def test_not_authorized_uses_linked_account_hint(make_client: Make) -> None:
    with pytest.raises(NotAuthorizedError) as exc:
        await user(make_client, lambda r: err(50)).starred_song_ids()
    assert "linked Navidrome account" in str(exc.value)
    assert "admin" not in str(exc.value)


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
    assert SALT not in str(exc.value)
    assert hashlib.md5((PASSWORD + SALT).encode()).hexdigest() not in str(exc.value)  # noqa: S324


async def test_play_counts_tolerates_missing_songs(make_client: Make) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["id"] == "gone":
            return err(70)
        return ok(song={"id": "s1", "playCount": 2})

    counts = await user(make_client, handler).play_counts(["s1", "gone"])
    assert counts == {"s1": 2}  # a vanished song is absent, not 0 plays
