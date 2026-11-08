"""ListenBrainz loves as a mirrored playlist: sync to Navidrome, confirm once, then auto-request."""

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mixsync.core.errors import PermanentError, TransientError
from mixsync.core.jobs import JobKind
from mixsync.core.types import RecordingRef
from mixsync.db.auth import create_user
from mixsync.db.models.discovery import MirroredPlaylist
from mixsync.db.models.work import Request
from mixsync.discovery import mirrored
from mixsync.discovery.loves import Loves

from .conftest import Env, login
from .test_discovery_playlists import rec
from .test_mirrored_playlists import REL, REL_2, MirrorRig, own_in_navidrome


class FakeLB:
    """ListenBrainzClient.loves: the loved recordings, newest first; `fail` raises like a 503."""

    def __init__(self) -> None:
        self.loved: list[int] = []
        self.users: list[str] = []
        self.fail = False

    async def loves(self, user: str) -> list[RecordingRef]:
        self.users.append(user)
        if self.fail:
            raise TransientError("ListenBrainz /1/feedback returned 503")
        return [RecordingRef(rec(n), None, "Artist", f"Loved {n}") for n in self.loved]


class LovesRig(MirrorRig):
    def __init__(self, env: Env) -> None:
        super().__init__(env)
        self.lb = FakeLB()
        self.mirror.upstreams[mirrored.LOVES] = Loves(self.base.disc, self.lb)  # type: ignore[arg-type]
        self.base.link(env.user_id, ("listenbrainz",))
        for n in range(1, 20):  # every loved recording is on REL (n < 10) or REL_2
            self.history.meta[rec(n)] = RecordingRef(rec(n), REL if n < 10 else REL_2, "A", "T")

    def create_loves(self, web: TestClient) -> int:
        tok = login(web, "alice")
        assert web.post("/mirrored/loves", data={"csrf_token": tok}).status_code == 303
        with self.env.sessions() as s:
            pid = s.scalar(select(MirroredPlaylist.id))
            assert pid
            return pid


@pytest.fixture
def lr(env: Env) -> LovesRig:
    return LovesRig(env)


async def test_loves_reach_navidrome_newest_first_without_resolving(
    lr: LovesRig, web: TestClient
) -> None:
    own_in_navidrome(lr, 1, 2, 3)
    pid = lr.create_loves(web)
    lr.lb.loved = [3, 1, 2]
    await lr.sync()
    assert lr.songs() == ["s3", "s1", "s2"]
    assert list(lr.lib.names.values()) == ["MixSync: Loves"]
    assert lr.lb.users == ["listenbrainz-name"]  # the linked ListenBrainz account
    assert lr.resolver.calls == []  # the MBIDs came from ListenBrainz
    pl = lr.pl(pid)
    assert (pl.cadence_days, pl.upstream_kind) == (1, mirrored.LOVES)
    assert [e["release_mbid"] for e in pl.entries] == [REL] * 3


async def test_first_sync_waits_then_new_loves_are_requested_automatically(
    lr: LovesRig, web: TestClient
) -> None:
    pid = lr.create_loves(web)
    lr.lb.loved = [2, 1]
    await lr.sync()
    assert lr.requests() == []  # the first sync only asks
    page = web.get("/mirrored").text
    assert "2 tracks will be requested" in page
    tok = login(web, "alice")
    web.post(f"/mirrored/{pid}/request", data={"csrf_token": tok, "release": [REL]})
    assert lr.pl(pid).auto_request_since is not None
    with lr.env.sessions.begin() as s:  # the first request ended without the tracks
        for q in s.scalars(select(Request)):
            q.status = "failed"

    lr.lb.loved = [11, 3, 2, 1]  # two new loves
    lr.env.clock.advance(timedelta(days=2))
    await lr.sync()
    new = [(q.release_mbid, q.recording_mbids, q.source, q.provisional) for q in lr.requests()]
    # REL also carries the failed 1 and 2: same release, one request
    assert sorted(new[1:]) == [
        (REL, [rec(3), rec(2), rec(1)], "playlist", False),
        (REL_2, [rec(11)], "playlist", False),
    ]

    lr.lb.loved = [12, 11, 3, 2, 1]  # 12 is on REL_2, whose request is still in flight
    lr.env.clock.advance(timedelta(days=2))
    await lr.sync()
    assert len(lr.requests()) == 3


async def test_a_mirror_of_another_kind_never_auto_requests(lr: LovesRig, web: TestClient) -> None:
    pid = lr.create()
    lr.upstream.lists["a"] = [mirrored.UpstreamTrack("Artist", "Upstream 2")]
    await lr.sync()
    tok = login(web, "alice")
    web.post(f"/mirrored/{pid}/request", data={"csrf_token": tok, "release": [REL]})
    assert lr.pl(pid).auto_request_since is None


async def test_a_listenbrainz_failure_raises_and_keeps_the_list(
    lr: LovesRig, web: TestClient
) -> None:
    own_in_navidrome(lr, 1)
    pid = lr.create_loves(web)
    lr.lb.loved = [1]
    await lr.sync()
    before = lr.pl(pid).entries
    lr.lb.fail = True
    lr.env.clock.advance(timedelta(days=2))
    with pytest.raises(TransientError):
        await lr.sync()
    assert lr.pl(pid).entries == before
    assert lr.lib.pushes == 1
    assert [j.status for j in lr.env.jobs(JobKind.MIRROR_SYNC)].count("queued") == 1


async def test_no_listenbrainz_link_raises(lr: LovesRig) -> None:
    with lr.env.sessions.begin() as s:
        bob = create_user(s, "bob", "pw").id
    with pytest.raises(PermanentError):
        await Loves(lr.base.disc, lr.lb).tracks(bob, "loves")  # type: ignore[arg-type]
    assert lr.lb.users == []


def test_the_page_offers_loves_once_and_only_with_a_link(lr: LovesRig, web: TestClient) -> None:
    with lr.env.sessions.begin() as s:
        create_user(s, "bob", "pw")
    tok = login(web, "bob")
    page = web.get("/mirrored").text
    assert "link ListenBrainz on your account page" in page
    assert 'action="/mirrored/loves"' not in page
    assert web.post("/mirrored/loves", data={"csrf_token": tok}).status_code == 400

    tok = login(web, "alice")
    assert 'action="/mirrored/loves"' in web.get("/mirrored").text
    for _ in range(2):  # a second click is a no-op
        assert web.post("/mirrored/loves", data={"csrf_token": tok}).status_code == 303
    assert len(lr.env.jobs(JobKind.MIRROR_SYNC)) == 1
    assert 'action="/mirrored/loves"' not in web.get("/mirrored").text


def test_a_playlist_already_named_loves_is_a_clear_conflict(lr: LovesRig, web: TestClient) -> None:
    with lr.env.sessions.begin() as s:
        mirrored.create(s, lr.env.clock.now(), lr.env.user_id, "Loves", "fake", "x")
    tok = login(web, "alice")
    assert web.post("/mirrored/loves", data={"csrf_token": tok}).status_code == 409


def test_no_last_fm_calls() -> None:
    src = Path(mirrored.__file__).parents[1]
    hits = [
        p
        for p in src.rglob("*.py")
        if any(h in p.read_text().lower() for h in ("audioscrobbler", "last.fm/", "lastfm.com"))
    ]
    assert hits == []
