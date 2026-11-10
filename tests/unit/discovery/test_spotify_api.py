"""The Spotify Web API client: PKCE, token refresh, playlist listing and item parsing."""

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.config import Settings
from mixsync.discovery.mirrored import UpstreamTrack
from mixsync.discovery.spotify_api import (
    API,
    TOKEN,
    Playlist,
    Revoked,
    SpotifyClient,
    authorize_url,
    pkce,
)
from mixsync.ratelimit.client import PoliteClient


class Clock:
    """Time moves only when the polite client sleeps, so its 1 req/s spacing costs nothing."""

    def __init__(self) -> None:
        self.t = datetime(2026, 1, 1, tzinfo=UTC)
        self.slept = 0.0

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.t += timedelta(seconds=seconds)


Handler = dict[str, httpx.Response]


def spotify(
    settings: Settings, sessions: sessionmaker[Session], routes: Handler
) -> tuple[SpotifyClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handle(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return routes[str(r.url)]

    clock = Clock()
    http = PoliteClient(
        "spotify",
        settings,
        sessions,
        clock=clock,
        transport=httpx.MockTransport(handle),
        sleep=clock.sleep,
    )
    return SpotifyClient(http, "cid"), seen


def track(n: int, isrc: str | None = None) -> dict[str, object]:
    return {
        "type": "track",
        "is_local": False,
        "name": f"Title {n} - Original Mix",
        "artists": [{"name": f"Artist {n}"}, {"name": "Guest"}],
        "external_ids": {"isrc": isrc} if isrc else {},
    }


def test_pkce_challenge_is_s256_and_the_scope_is_read_only() -> None:
    verifier, challenge = pkce()
    assert 43 <= len(verifier) <= 128
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    q = parse_qs(urlsplit(authorize_url("cid", "http://127.0.0.1:8000/cb", "st", challenge)).query)
    assert q == {
        "response_type": ["code"],
        "client_id": ["cid"],
        "scope": ["playlist-read-private"],
        "redirect_uri": ["http://127.0.0.1:8000/cb"],
        "state": ["st"],
        "code_challenge_method": ["S256"],
        "code_challenge": [challenge],
    }


async def test_refresh_keeps_the_old_token_when_none_comes_back(
    settings: Settings, sessions: sessionmaker[Session]
) -> None:
    client, seen = spotify(
        settings, sessions, {TOKEN: httpx.Response(200, json={"access_token": "A"})}
    )
    assert await client.refresh("R1") == ("A", "R1")
    form = parse_qs(seen[0].content.decode())
    assert form == {"grant_type": ["refresh_token"], "refresh_token": ["R1"], "client_id": ["cid"]}

    rotated = {TOKEN: httpx.Response(200, json={"access_token": "B", "refresh_token": "R2"})}
    client, _ = spotify(settings, sessions, rotated)
    assert await client.refresh("R1") == ("B", "R2")


async def test_invalid_grant_is_revoked_and_never_echoes_the_token(
    settings: Settings, sessions: sessionmaker[Session]
) -> None:
    refused = {TOKEN: httpx.Response(400, json={"error": "invalid_grant"})}
    client, _ = spotify(settings, sessions, refused)
    with pytest.raises(Revoked) as e:
        await client.refresh("secret-refresh-token")
    assert "secret-refresh-token" not in str(e.value)


async def test_playlists_keep_own_and_collaborative_and_follow_pages(
    settings: Settings, sessions: sessionmaker[Session]
) -> None:
    page2 = f"{API}/me/playlists?offset=50&limit=50"
    routes = {
        f"{API}/me/playlists?limit=50": httpx.Response(
            200,
            json={
                "items": [
                    {"id": "mine1", "name": "Mine", "owner": {"id": "me"}, "items": {"total": 3}},
                    {"id": "theirs", "name": "Followed", "owner": {"id": "x"}},
                ],
                "next": page2,
            },
        ),
        page2: httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "collab1",
                        "name": "Shared",
                        "owner": {"id": "x"},
                        "collaborative": True,
                        "tracks": {"total": 7},  # the deprecated count still works
                    }
                ],
                "next": "https://evil.example/steal",  # never followed with the token
            },
        ),
    }
    client, seen = spotify(settings, sessions, routes)
    assert await client.playlists("AT", "me") == [
        Playlist("mine1", "Mine", 3),
        Playlist("collab1", "Shared", 7),
    ]
    assert len(seen) == 2
    assert all(r.headers["Authorization"] == "Bearer AT" for r in seen)


async def test_items_parse_item_or_track_and_skip_local_null_and_episodes(
    settings: Settings, sessions: sessionmaker[Session]
) -> None:
    items = [
        {"item": track(1, "gbabc1234567")},
        {"track": track(2)},  # the deprecated key
        {"item": None},  # a removed track
        {"is_local": True, "item": {**track(3), "is_local": True}},
        {"item": {**track(4), "type": "episode"}},
    ]
    url = f"{API}/playlists/abc123/items?limit=50"
    client, _ = spotify(settings, sessions, {url: httpx.Response(200, json={"items": items})})
    assert await client.tracks("AT", "abc123") == [
        UpstreamTrack("Artist 1", "Title 1 - Original Mix", "GBABC1234567"),
        UpstreamTrack("Artist 2", "Title 2 - Original Mix"),
    ]
