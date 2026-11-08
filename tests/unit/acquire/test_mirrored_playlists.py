"""Mirrored playlists: upstream sync, confirm-gated requests, unresolved tracks, the web page."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mixsync.core.jobs import JobKind, JobState
from mixsync.core.types import RecordingRef
from mixsync.db.auth import create_user
from mixsync.db.models.discovery import MirroredPlaylist
from mixsync.db.models.library import Track
from mixsync.db.models.safety import FileOp
from mixsync.db.models.work import Job, Request
from mixsync.discovery import mirrored
from mixsync.discovery.mirrored import Mirror, UpstreamTrack

from .conftest import Env, login
from .test_discovery_playlists import FakeLib, Rig, own, rec

REL = "11111111-0000-0000-0000-000000000001"
REL_2 = "11111111-0000-0000-0000-000000000002"


def up(n: int, isrc: str | None = None) -> UpstreamTrack:
    return UpstreamTrack("Artist", f"Upstream {n}", isrc)


class FakeUpstream:
    def __init__(self) -> None:
        self.lists: dict[str, list[UpstreamTrack]] = {}

    async def tracks(self, user_id: int, ref: str) -> list[UpstreamTrack]:
        return self.lists[ref]


class FakeResolver:
    """Upstream n resolves to rec(n) on REL; listed titles stay unresolved."""

    def __init__(self) -> None:
        self.unresolved: set[str] = set()
        self.no_release: set[str] = set()
        self.calls: list[str] = []

    async def __call__(self, artist: str, title: str, isrc: str | None) -> RecordingRef | None:
        self.calls.append(title)
        if title in self.unresolved:
            return None
        n = int(title.split()[-1])
        release = None if title in self.no_release else (REL_2 if n >= 10 else REL)
        return RecordingRef(rec(n), release, artist, f"Title {n}")


class CountingLib(FakeLib):
    def __init__(self) -> None:
        super().__init__()
        self.pushes = 0

    async def push_playlist(self, name: str, song_ids: list[str], existing_id: str | None) -> str:
        self.pushes += 1
        return await super().push_playlist(name, song_ids, existing_id)


class MirrorRig:
    def __init__(self, env: Env) -> None:
        self.base = Rig(env)
        self.base.lib = self.lib = CountingLib()
        self.env, self.history = env, self.base.history
        self.upstream, self.resolver = FakeUpstream(), FakeResolver()
        self.mirror = Mirror(self.base.disc, self.resolver, {"fake": self.upstream})
        self.base.link(env.user_id, ("navidrome",))

    def create(self, user_id: int | None = None, ref: str = "a") -> int:
        with self.env.sessions.begin() as s:
            pl = mirrored.create(
                s, self.env.clock.now(), user_id or self.env.user_id, f"Mix {ref}", "fake", ref
            )
            return pl.id

    async def sync(self) -> None:
        """Run the oldest queued sync, as the worker would."""
        with self.env.sessions.begin() as s:
            j = s.scalars(
                select(Job)
                .where(Job.kind == JobKind.MIRROR_SYNC, Job.status == "queued")
                .order_by(Job.id)
            ).first()
            assert j
            j.status, jid = JobState.SUCCEEDED, j.id
        await self.mirror.sync(self.env.queue.get(jid))

    def pl(self, pid: int) -> MirroredPlaylist:
        with self.env.sessions() as s:
            pl = s.get(MirroredPlaylist, pid)
            assert pl
            s.expunge(pl)
            return pl

    def requests(self) -> list[Request]:
        with self.env.sessions() as s:
            return list(s.scalars(select(Request).order_by(Request.id)))

    def songs(self) -> list[str]:
        (songs,) = self.lib.playlists.values()
        return songs


@pytest.fixture
def mr(env: Env) -> MirrorRig:
    return MirrorRig(env)


def own_in_navidrome(mr: MirrorRig, *ns: int) -> None:
    own(mr.env, *ns)
    for n in ns:
        mr.lib.main[rec(n)] = f"s{n}"


async def test_resync_applies_upstream_adds_and_removes_in_order_without_file_ops(
    mr: MirrorRig,
) -> None:
    own_in_navidrome(mr, 1, 2, 3, 4)
    pid = mr.create()
    mr.upstream.lists["a"] = [up(1), up(2), up(3)]
    await mr.sync()
    assert mr.songs() == ["s1", "s2", "s3"]
    assert [n for n in mr.lib.names.values()] == ["MixSync: Mix a"]
    mr.upstream.lists["a"] = [up(4), up(3), up(1)]  # 2 left upstream, 4 joined, order changed
    mr.env.clock.advance(timedelta(days=2))
    await mr.sync()
    assert mr.songs() == ["s4", "s3", "s1"]
    assert len(mr.lib.playlists) == 1  # replaced, not a second playlist
    assert [e["title"] for e in mr.pl(pid).entries] == ["Upstream 4", "Upstream 3", "Upstream 1"]
    with mr.env.sessions() as s:
        assert s.scalars(select(FileOp)).first() is None
        assert rec(2) in set(s.scalars(select(Track.recording_mbid)))  # its file stays
    assert mr.resolver.calls.count("Upstream 1") == 1  # resolved once, then remembered


async def test_missing_tracks_are_requested_only_on_confirmation(
    mr: MirrorRig, web: TestClient
) -> None:
    own_in_navidrome(mr, 1)
    pid = mr.create()
    mr.upstream.lists["a"] = [up(1), up(2), up(3), up(10)]
    await mr.sync()
    assert mr.requests() == []
    assert mr.songs() == ["s1"]
    tok = login(web, "alice")
    page = web.get("/mirrored").text
    assert "3 tracks will be requested" in page
    assert "edits made there are overwritten" in page
    # only the releases the page listed are requested
    r = web.post(f"/mirrored/{pid}/request", data={"csrf_token": tok, "release": [REL]})
    assert r.status_code == 303
    reqs = mr.requests()
    assert [(q.release_mbid, q.recording_mbids, q.source, q.provisional) for q in reqs] == [
        (REL, [rec(2), rec(3)], "playlist", False)
    ]
    assert "1 track will be requested" in web.get("/mirrored").text
    web.post(f"/mirrored/{pid}/request", data={"csrf_token": tok, "release": [REL, REL_2]})
    assert [q.release_mbid for q in mr.requests()] == [REL, REL_2]  # REL is not asked again
    assert "will be requested" not in web.get("/mirrored").text


async def test_unresolved_tracks_are_listed_left_out_of_the_push_and_retried(
    mr: MirrorRig, web: TestClient
) -> None:
    own_in_navidrome(mr, 1, 2)
    mr.create()
    mr.upstream.lists["a"] = [up(1), up(2)]
    mr.resolver.unresolved = {"Upstream 2"}
    await mr.sync()
    assert mr.songs() == ["s1"]
    login(web, "alice")
    page = web.get("/mirrored").text
    assert "Upstream 2" in page and "Unresolved" in page
    assert "will be requested" not in page
    mr.resolver.unresolved = set()
    mr.env.clock.advance(timedelta(days=2))
    await mr.sync()
    assert mr.resolver.calls.count("Upstream 2") == 2
    assert mr.songs() == ["s1", "s2"]


async def test_an_isrc_match_gets_its_release_from_listenbrainz(mr: MirrorRig) -> None:
    pid = mr.create()
    mr.upstream.lists["a"] = [up(5, "USRC17607839")]
    mr.resolver.no_release = {"Upstream 5"}
    mr.history.meta[rec(5)] = RecordingRef(rec(5), REL_2, "Artist", "Title 5")
    await mr.sync()
    (e,) = mr.pl(pid).entries
    assert (e["recording_mbid"], e["release_mbid"], e["isrc"]) == (rec(5), REL_2, "USRC17607839")


async def test_the_chain_reschedules_on_cadence_and_without_an_adapter(mr: MirrorRig) -> None:
    pid = mr.create()
    with mr.env.sessions.begin() as s:
        pl = s.get(MirroredPlaylist, pid)
        assert pl
        pl.upstream_kind = "gone"
    mr.mirror.start_chains()  # already queued: no second sync
    assert len(mr.env.jobs(JobKind.MIRROR_SYNC)) == 1
    await mr.sync()
    pl = mr.pl(pid)
    assert pl.last_error == "no upstream adapter for 'gone'"
    queued = [j for j in mr.env.jobs(JobKind.MIRROR_SYNC) if j.status == "queued"]
    assert len(queued) == 1
    assert queued[0].run_after.replace(tzinfo=None) == (
        mr.env.clock.now() + timedelta(days=1)
    ).replace(tzinfo=None)  # Mid() jitter is 0


def test_create_is_idempotent_per_upstream(mr: MirrorRig) -> None:
    assert mr.create() == mr.create()
    assert len(mr.env.jobs(JobKind.MIRROR_SYNC)) == 1


def test_every_route_is_owner_only(mr: MirrorRig, web: TestClient) -> None:
    pid = mr.create()
    with mr.env.sessions.begin() as s:
        create_user(s, "bob", "pw")
    tok = login(web, "bob")
    assert "Mix a" not in web.get("/mirrored").text
    assert web.get(f"/mirrored/{pid}/delete").status_code == 403
    for path, data in (
        ("settings", {"cadence_days": "2"}),
        ("sync", {}),
        ("request", {"release": REL}),
        ("delete", {}),
    ):
        assert (
            web.post(f"/mirrored/{pid}/{path}", data={"csrf_token": tok, **data}).status_code == 403
        )
    assert web.post("/mirrored/999/sync", data={"csrf_token": tok}).status_code == 404
    assert mr.pl(pid).cadence_days == 1


def test_cadence_is_a_whole_number_of_at_least_one_day(mr: MirrorRig, web: TestClient) -> None:
    pid = mr.create()
    tok = login(web, "alice")
    for bad in ("0", "", "1.5", "-1", "abc", "366"):
        r = web.post(f"/mirrored/{pid}/settings", data={"csrf_token": tok, "cadence_days": bad})
        assert r.status_code == 400, bad
    r = web.post(f"/mirrored/{pid}/settings", data={"csrf_token": tok, "cadence_days": " 3 "})
    assert r.status_code == 303
    assert mr.pl(pid).cadence_days == 3


def test_sync_now_brings_the_sync_forward_once_an_hour(mr: MirrorRig, web: TestClient) -> None:
    pid = mr.create()
    with mr.env.sessions.begin() as s:
        pl = s.get(MirroredPlaylist, pid)
        assert pl
        pl.last_synced_at = mr.env.clock.now() - timedelta(days=1)
    tok = login(web, "alice")
    assert web.post(f"/mirrored/{pid}/sync", data={"csrf_token": tok}).status_code == 303
    assert len(mr.env.jobs(JobKind.MIRROR_SYNC)) == 1  # moved, not doubled
    with mr.env.sessions.begin() as s:
        pl = s.get(MirroredPlaylist, pid)
        assert pl
        pl.last_synced_at = pl.next_sync_at
    assert web.post(f"/mirrored/{pid}/sync", data={"csrf_token": tok}).status_code == 409


async def test_delete_removes_the_mirror_only(mr: MirrorRig, web: TestClient) -> None:
    own_in_navidrome(mr, 1)
    pid = mr.create()
    mr.upstream.lists["a"] = [up(1)]
    await mr.sync()
    tok = login(web, "alice")
    assert "No files are deleted" in web.get(f"/mirrored/{pid}/delete").text
    assert web.post(f"/mirrored/{pid}/delete", data={"csrf_token": tok}).status_code == 303
    with mr.env.sessions() as s:
        assert s.get(MirroredPlaylist, pid) is None
        assert s.scalars(select(Track)).first() is not None
    assert mr.songs() == ["s1"]  # the Navidrome playlist is left as it is


async def test_an_empty_upstream_keeps_the_last_list(mr: MirrorRig) -> None:
    own_in_navidrome(mr, 1, 2)
    pid = mr.create()
    mr.upstream.lists["a"] = [up(1), up(2)]
    await mr.sync()
    before = mr.pl(pid).entries
    mr.upstream.lists["a"] = []
    mr.env.clock.advance(timedelta(days=2))
    await mr.sync()
    pl = mr.pl(pid)
    assert pl.entries == before
    assert pl.last_error == mirrored.EMPTY_UPSTREAM
    assert mr.lib.pushes == 1 and mr.songs() == ["s1", "s2"]
    assert [j.status for j in mr.env.jobs(JobKind.MIRROR_SYNC)].count("queued") == 1


async def test_an_empty_upstream_on_a_new_mirror_is_fine(mr: MirrorRig) -> None:
    pid = mr.create()
    mr.upstream.lists["a"] = []
    await mr.sync()
    pl = mr.pl(pid)
    assert pl.entries == [] and pl.last_error is None and pl.last_synced_at is not None


async def test_an_ended_request_does_not_block_asking_again(mr: MirrorRig, web: TestClient) -> None:
    pid = mr.create()
    mr.upstream.lists["a"] = [up(2), up(10)]
    await mr.sync()
    with mr.env.sessions.begin() as s:
        s.add(Request(user_id=mr.env.user_id, release_mbid=REL, status="failed"))
        # imported, yet the track never landed in the library
        s.add(Request(user_id=mr.env.user_id, release_mbid=REL_2, status="imported"))
    tok = login(web, "alice")
    assert "2 tracks will be requested" in web.get("/mirrored").text
    r = web.post(f"/mirrored/{pid}/request", data={"csrf_token": tok, "release": [REL, REL_2]})
    assert r.status_code == 303
    new = [(q.release_mbid, q.source) for q in mr.requests()[2:]]
    assert new == [(REL, "playlist"), (REL_2, "playlist")]
    assert "will be requested" not in web.get("/mirrored").text  # in flight now


async def test_a_push_is_skipped_when_navidrome_finds_none_of_the_owned_tracks(
    mr: MirrorRig,
) -> None:
    own(mr.env, 1, 2)  # in the library, but Navidrome has not scanned them
    pid = mr.create()
    mr.upstream.lists["a"] = [up(1), up(2)]
    await mr.sync()
    pl = mr.pl(pid)
    assert mr.lib.pushes == 0 and pl.navidrome_playlist_id is None
    assert pl.last_error == mirrored.NONE_FOUND
