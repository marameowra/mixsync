"""Spotify playlist import (#337) through the web: connect with PKCE, import, never request,
reconnect on a refused token, disconnect. Imports go stale after 6 months unless the worker's
daily pass imports them again first (#416)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mixsync.core.clock import aware
from mixsync.core.jobs import JobKind, JobState
from mixsync.db.auth import create_user
from mixsync.db.models.accounts import UserLink
from mixsync.db.models.discovery import MirroredPlaylist
from mixsync.db.models.safety import FileOp
from mixsync.db.models.work import Job, Request
from mixsync.discovery import mirrored, spotify_api
from mixsync.discovery.mirrored import UpstreamTrack
from mixsync.discovery.spotify_api import (
    KIND,
    REIMPORT_WINDOW,
    STALE,
    STALE_AFTER,
    Reimport,
    SpotifyClient,
    Stored,
    is_stale,
)
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
        self.items_status = 200  # of the playlist's items

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
        if self.items_status != 200:
            return httpx.Response(self.items_status, json={})
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
    app.state.services = replace(services, settings=settings, spotify_http=http, clock=env.clock)
    return fake


@pytest.fixture
def mr(env: Env) -> MirrorRig:
    rig = MirrorRig(env)
    rig.mirror.upstreams[KIND] = Stored(env.sessions, env.clock)
    return rig


@pytest.fixture
def bg(web: TestClient, env: Env, fake: FakeSpotify) -> Reimport:
    """The worker's daily re-import pass, on the web app's key and polite client."""
    services = web.app.state.services  # type: ignore[attr-defined]
    client = SpotifyClient(services.spotify_http, "cid")
    rig = Reimport(env.sessions, env.clock, services.secret_box, client, rng=lambda a, b: 1.0)
    rig.start_chains()
    return rig


async def daily(env: Env, bg: Reimport) -> None:
    """Run the queued daily pass, as the worker would."""
    with env.sessions.begin() as s:
        (j,) = s.scalars(
            select(Job).where(Job.kind == JobKind.SPOTIFY_REIMPORT, Job.status == "queued")
        )
        j.status, jid = JobState.SUCCEEDED, j.id
    await bg.run(env.queue.get(jid))


def imported(env: Env, at: datetime, pid: int | None = None) -> None:
    """Date the (only, or `pid`) Spotify import back to `at`, as an import then would have."""
    with env.sessions.begin() as s:
        q = select(MirroredPlaylist).where(MirroredPlaylist.upstream_kind == KIND)
        pl = s.get(MirroredPlaylist, pid) if pid else s.scalars(q).one()
        assert pl
        pl.imported_at, pl.reimport_after = at, at + STALE_AFTER - REIMPORT_WINDOW


def item_calls(fake: FakeSpotify) -> int:
    return sum(r.url.path.startswith("/v1/playlists/") for r in fake.calls)


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
    web: TestClient, env: Env, fake: FakeSpotify, mr: MirrorRig, bg: Reimport
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
    # the worker's background re-import and the sync it queues request nothing either (#416)
    imported(env, env.clock.now())
    env.clock.advance(STALE_AFTER - REIMPORT_WINDOW)
    fake.tracks = [1, 2, 3]
    await daily(env, bg)
    await mr.sync()
    assert len(mr.pl(pl.id).entries) == 3
    assert requests(env) == []
    assert env.jobs(JobKind.SEARCH) == [] and env.jobs(JobKind.DOWNLOAD) == []  # no source


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


def only(env: Env, pid: int | None = None) -> MirroredPlaylist:
    """The (only, or `pid`) Spotify import."""
    with env.sessions() as s:
        q = select(MirroredPlaylist).where(MirroredPlaylist.upstream_kind == KIND)
        pl = s.get(MirroredPlaylist, pid) if pid else s.scalars(q).one()
        assert pl
        s.expunge(pl)
        return pl


def imported_at(env: Env, pid: int | None = None) -> datetime | None:
    at = only(env, pid).imported_at
    return at and aware(at)


async def test_an_import_goes_stale_at_six_months_and_a_new_import_clears_it(
    web: TestClient, env: Env, fake: FakeSpotify, bg: Reimport
) -> None:
    tok = connected(web, fake)
    form = {"csrf_token": tok, "playlist_id": "pl1", "name": "Faves"}
    web.post("/spotify/import", data=form)
    start = env.clock.now()
    imported(env, start)
    env.clock.advance(STALE_AFTER - timedelta(seconds=1))
    assert not is_stale(only(env), env.clock.now())
    env.clock.advance(timedelta(seconds=1))
    assert is_stale(only(env), env.clock.now())
    # Import again from the page starts the clock over
    assert web.post("/spotify/import", data=form).status_code == 303
    assert imported_at(env) == env.clock.now() and not is_stale(only(env), env.clock.now())
    page = web.get("/mirrored").text
    assert "Stale" not in page and f"Goes stale on {(env.clock.now() + STALE_AFTER).date()}" in page
    # and so does a background re-import
    imported(env, start)
    await daily(env, bg)
    assert imported_at(env) == env.clock.now() and not is_stale(only(env), env.clock.now())
    assert [e["title"] for e in only(env).entries] == ["Upstream 1", "Upstream 2"]


async def test_a_stale_import_keeps_only_its_owned_matches_and_is_not_synced(
    web: TestClient, env: Env, fake: FakeSpotify, mr: MirrorRig, bg: Reimport
) -> None:
    own_in_navidrome(mr, 1)
    fake.tracks = [1, 2, 3]
    mr.resolver.unresolved = {"Upstream 3"}
    tok = connected(web, fake)
    web.post("/spotify/import", data={"csrf_token": tok, "playlist_id": "pl1", "name": "Faves"})
    await mr.sync()
    assert mr.songs() == ["s1"]
    pid = only(env).id
    other = mr.create()  # another kind of mirror
    mr.upstream.lists["a"] = [UpstreamTrack("Artist", "Upstream 1")]
    await mr.sync()
    with env.sessions.begin() as s:  # another user's import, still fresh
        bob = create_user(s, "bob", "pw").id
        tracks = [UpstreamTrack("Artist", "Upstream 1")]
        later = env.clock.now() + STALE_AFTER
        bobs = spotify_api.save(s, later, bob, "pl9", "Bob", tracks).id
    before = {p: mr.pl(p).entries for p in (other, bobs)}
    imported(env, env.clock.now(), pid)
    env.clock.advance(STALE_AFTER)
    fake.revoked = True  # the background re-import fails
    await daily(env, bg)
    (entry,) = mr.pl(pid).entries  # the owned match only; the missing and unresolved are gone
    assert entry == {
        "recording_mbid": entry["recording_mbid"],
        "release_mbid": entry["release_mbid"],
        "rec_artist": "Artist",
        "rec_title": "Title 1",
    }  # no Spotify artist, title or ISRC
    assert entry["recording_mbid"]
    assert mr.pl(pid).navidrome_playlist_id
    assert {p: mr.pl(p).entries for p in (other, bobs)} == before
    pushes = mr.lib.pushes
    with env.sessions.begin() as s:
        pl = s.get(MirroredPlaylist, pid)
        assert pl
        mirrored.sync_now(s, env.clock.now(), pl, force=True)
        (job,) = s.scalars(mirrored.waiting(pid))
        jid = job.id
    await mr.mirror.sync(env.queue.get(jid))
    assert mr.lib.pushes == pushes and mr.songs() == ["s1"]  # not re-synced, playlist frozen
    assert mr.pl(pid).last_error == STALE
    with env.sessions() as s:
        assert s.scalar(select(FileOp.id)) is None  # no file touched
    page = web.get("/mirrored").text
    assert "Stale" in page and "Artist - Title 1" in page and "Upstream 1" not in page


async def test_the_worker_reimports_only_mirrors_near_stale_at_most_once_a_day(
    web: TestClient, env: Env, fake: FakeSpotify, bg: Reimport
) -> None:
    connected(web, fake)
    start = env.clock.now()
    tracks = [UpstreamTrack("Artist", "Upstream 1")]
    with env.sessions.begin() as s:
        due = spotify_api.save(s, start, env.user_id, "pl1", "Due", tracks).id
        nxt = start + timedelta(days=1)
        later = spotify_api.save(s, nxt, env.user_id, "pl2", "Later", tracks).id
    env.clock.advance(STALE_AFTER - REIMPORT_WINDOW - timedelta(seconds=1))
    await daily(env, bg)
    assert item_calls(fake) == 0  # nothing within 14 days of stale yet
    env.clock.advance(timedelta(seconds=1))
    fake.items_status = 500  # Spotify down: tried again tomorrow, not before
    await daily(env, bg)
    assert item_calls(fake) == 1
    await daily(env, bg)
    assert item_calls(fake) == 1
    env.clock.advance(timedelta(days=1))
    fake.items_status = 200
    await daily(env, bg)
    assert item_calls(fake) == 3  # the first again, and the second, now due too
    assert imported_at(env, due) == imported_at(env, later) == env.clock.now()
    await daily(env, bg)
    assert item_calls(fake) == 3


async def test_a_refused_token_in_the_background_asks_to_reconnect_without_retrying(
    web: TestClient, env: Env, fake: FakeSpotify, bg: Reimport
) -> None:
    tok = connected(web, fake)
    web.post("/spotify/import", data={"csrf_token": tok, "playlist_id": "pl1", "name": "Faves"})
    imported(env, env.clock.now())
    env.clock.advance(STALE_AFTER - REIMPORT_WINDOW)
    fake.revoked = True
    calls = len(fake.calls)
    await daily(env, bg)
    assert len(fake.calls) == calls + 1  # the one refresh, refused
    row = link(env)
    assert row and row.needs_relink
    for _ in range(14):
        env.clock.advance(timedelta(days=1))
        await daily(env, bg)
    assert len(fake.calls) == calls + 1  # no retry with a dead token
    assert is_stale(only(env), env.clock.now())  # stale on schedule
    assert all("artist" not in e for e in only(env).entries)


@pytest.mark.parametrize("status", [403, 404])
async def test_a_playlist_spotify_refuses_is_not_retried_and_goes_stale(
    web: TestClient, env: Env, fake: FakeSpotify, bg: Reimport, status: int
) -> None:
    tok = connected(web, fake)
    web.post("/spotify/import", data={"csrf_token": tok, "playlist_id": "pl1", "name": "Faves"})
    imported(env, env.clock.now())
    env.clock.advance(STALE_AFTER - REIMPORT_WINDOW)
    fake.items_status = status
    calls = item_calls(fake)
    await daily(env, bg)
    assert item_calls(fake) == calls + 1
    for _ in range(20):
        env.clock.advance(timedelta(days=1))
        await daily(env, bg)
    assert item_calls(fake) == calls + 1
    assert is_stale(only(env), env.clock.now())
    assert all("artist" not in e for e in only(env).entries)
