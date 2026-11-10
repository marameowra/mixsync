"""Spotify playlist import (#337) through the web: connect with PKCE, import, never request,
reconnect on a refused token, disconnect."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mixsync.db.models.accounts import UserLink
from mixsync.db.models.discovery import MirroredPlaylist
from mixsync.db.models.safety import FileOp
from mixsync.db.models.work import Request
from mixsync.discovery import mirrored
from mixsync.discovery.spotify_api import KIND, Stored
from mixsync.ratelimit.client import PoliteClient

from .conftest import Env, login
from .test_mirrored_playlists import MirrorRig, own_in_navidrome

API = "https://api.spotify.com/v1"


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class FakeSpotify:
    """accounts.spotify.com and api.spotify.com, as far as the import uses them."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.forms: list[dict[str, list[str]]] = []
        self.revoked = False
        self.tracks = [1, 2]

    def __call__(self, r: httpx.Request) -> httpx.Response:
        self.calls.append(r)
        if r.url.host == "accounts.spotify.com":
            form = parse_qs(r.content.decode())
            self.forms.append(form)
            if form["grant_type"] == ["authorization_code"]:
                return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT1"})
            if self.revoked:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "AT2"})  # no rotation
        if r.url.path == "/v1/me":
            return httpx.Response(200, json={"id": "spotme"})
        if r.url.path == "/v1/me/playlists":
            mine = {"id": "pl1", "name": "Faves", "owner": {"id": "spotme"}, "items": {"total": 2}}
            return httpx.Response(200, json={"items": [mine], "next": None})
        items = [
            {"item": {"type": "track", "name": f"Upstream {n}", "artists": [{"name": "Artist"}]}}
            for n in self.tracks
        ]
        return httpx.Response(200, json={"items": items, "next": None})


@pytest.fixture
def fake(web: TestClient, env: Env) -> FakeSpotify:
    fake, clock = FakeSpotify(), Clock()
    http = PoliteClient(
        "spotify",
        env.settings,
        env.sessions,
        clock=clock,
        transport=httpx.MockTransport(fake),
        sleep=clock.sleep,
    )
    app = web.app  # type: ignore[attr-defined]
    services = app.state.services
    settings = services.settings.model_copy(update={"spotify_client_id": "cid"})
    app.state.services = replace(services, settings=settings, spotify_http=http)
    return fake


@pytest.fixture
def mr(env: Env) -> MirrorRig:
    rig = MirrorRig(env)
    rig.mirror.upstreams[KIND] = Stored(env.sessions)
    return rig


def connect(web: TestClient, tok: str) -> str:
    """Start the OAuth dance; returns the state Spotify would echo back."""
    r = web.post("/spotify/connect", data={"csrf_token": tok})
    assert r.status_code == 303
    assert r.headers["location"].startswith("https://accounts.spotify.com/authorize?")
    return parse_qs(urlsplit(r.headers["location"]).query)["state"][0]


def link(env: Env) -> UserLink | None:
    with env.sessions() as s:
        row = s.scalar(select(UserLink).where(UserLink.service == "spotify"))
        if row:
            s.expunge(row)
        return row


def requests(env: Env) -> list[Request]:
    with env.sessions() as s:
        return list(s.scalars(select(Request)))


def connected(web: TestClient, fake: FakeSpotify) -> str:
    tok = login(web, "alice")
    state = connect(web, tok)
    assert web.get(f"/spotify/callback?code=CODE&state={state}").status_code == 303
    return tok


def test_connect_exchanges_the_code_with_the_verifier_and_stores_the_refresh_token(
    web: TestClient, env: Env, fake: FakeSpotify
) -> None:
    tok = login(web, "alice")
    r = web.post("/spotify/connect", data={"csrf_token": tok})
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["scope"] == ["playlist-read-private"] and q["code_challenge_method"] == ["S256"]
    redirect = q["redirect_uri"][0]
    assert redirect.endswith("/spotify/callback")
    assert web.get(f"/spotify/callback?code=CODE&state={q['state'][0]}").status_code == 303
    (form,) = fake.forms
    assert form["code"] == ["CODE"] and form["redirect_uri"] == [redirect]
    assert form["client_id"] == ["cid"] and "client_secret" not in form
    assert len(form["code_verifier"][0]) >= 43
    row = link(env)
    assert row and row.external_username == "spotme" and not row.needs_relink
    assert "RT1" not in row.secret_encrypted  # stored encrypted
    page = web.get("/account").text
    assert "Connected as spotme" in page and redirect in page
    assert "RT1" not in page


def test_a_callback_with_another_state_or_session_links_nothing(
    web: TestClient, env: Env, fake: FakeSpotify
) -> None:
    tok = login(web, "alice")
    state = connect(web, tok)
    assert web.get("/spotify/callback?code=CODE&state=forged").status_code == 400
    # the same state after logging in again (a new session) is refused too
    login(web, "alice")
    assert web.get(f"/spotify/callback?code=CODE&state={state}").status_code == 400
    assert link(env) is None and fake.forms == []


async def test_import_builds_the_playlist_from_owned_tracks_and_only_lists_missing_ones(
    web: TestClient, env: Env, fake: FakeSpotify, mr: MirrorRig
) -> None:
    own_in_navidrome(mr, 1)
    tok = connected(web, fake)
    page = web.get("/spotify/playlists")
    assert page.status_code == 200 and "Faves" in page.text
    r = web.post("/spotify/import", data={"csrf_token": tok, "playlist_id": "pl1", "name": "Faves"})
    assert r.status_code == 303
    calls = len(fake.calls)
    await mr.sync()
    assert len(fake.calls) == calls  # the worker's sync never calls Spotify
    assert mr.songs() == ["s1"]
    with env.sessions() as s:
        (pl,) = s.scalars(select(MirroredPlaylist))
        assert pl.upstream_kind == KIND
        states = [state for _, state in mirrored.track_states(s, pl)]
        pid = pl.id
    assert states == [mirrored.OWNED, mirrored.MISSING]
    text = web.get("/mirrored").text
    assert "Artist - Upstream 2" in text
    assert f"/mirrored/{pid}/request" not in text  # no request button
    assert "will be requested" not in text


async def test_a_spotify_import_never_creates_a_request(
    web: TestClient, env: Env, fake: FakeSpotify, mr: MirrorRig
) -> None:
    tok = connected(web, fake)
    web.post("/spotify/import", data={"csrf_token": tok, "playlist_id": "pl1", "name": "Faves"})
    await mr.sync()
    with env.sessions() as s:
        pl = s.scalars(select(MirroredPlaylist)).one()
        releases = [e["release_mbid"] for e in pl.entries]
        assert any(state == mirrored.MISSING for _, state in mirrored.track_states(s, pl))
    r = web.post(f"/mirrored/{pl.id}/request", data={"csrf_token": tok, "release": releases})
    assert r.status_code == 403
    assert requests(env) == []
    with env.sessions() as s:
        assert s.get(MirroredPlaylist, pl.id).auto_request_since is None  # type: ignore[union-attr]


def test_a_refused_refresh_token_asks_to_reconnect(
    web: TestClient, env: Env, fake: FakeSpotify
) -> None:
    tok = connected(web, fake)
    fake.revoked = True
    r = web.post("/spotify/import", data={"csrf_token": tok, "playlist_id": "pl1", "name": "Faves"})
    assert r.status_code == 400
    row = link(env)
    assert row and row.needs_relink
    assert "Reconnect Spotify" in web.get("/account").text
    calls = len(fake.calls)
    assert web.get("/spotify/playlists").status_code == 400  # no retry with a dead token
    assert len(fake.calls) == calls


async def test_disconnect_deletes_the_login_and_imported_playlists_only(
    web: TestClient, env: Env, fake: FakeSpotify, mr: MirrorRig
) -> None:
    own_in_navidrome(mr, 1)
    tok = connected(web, fake)
    web.post("/spotify/import", data={"csrf_token": tok, "playlist_id": "pl1", "name": "Faves"})
    await mr.sync()
    other = mr.create()  # another kind of mirror stays
    assert web.post("/spotify/disconnect", data={"csrf_token": tok}).status_code == 303
    assert link(env) is None
    with env.sessions() as s:
        assert [p.id for p in s.scalars(select(MirroredPlaylist))] == [other]
        assert s.scalar(select(FileOp.id)) is None  # no file touched
    assert mr.songs() == ["s1"]  # the Navidrome playlist is left as it is
    assert "Disconnect Spotify" not in web.get("/account").text


def test_spotify_is_hidden_without_a_client_id(web: TestClient) -> None:
    login(web, "alice")
    assert "Spotify app" not in web.get("/account").text
    assert web.get("/spotify/playlists").status_code == 404
