"""Radio: nearest owned, analyzed tracks per seed kind with artist and album caps, the refusal,
the reasons, the rebuild cycle and push shared with smart playlists, seeds that never follow a
reused id, and the web pages. Analyses are hand-made rows; nothing here runs bliss."""

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from mixsync.core.jobs import JobKind, JobState
from mixsync.core.types import LibrarySong
from mixsync.db.app_settings import switch_media_server
from mixsync.db.auth import create_user
from mixsync.db.models.discovery import (
    DiscoveryPlaylist,
    MirroredPlaylist,
    RadioPlaylist,
    SmartPlaylist,
)
from mixsync.db.models.library import Track, TrackAnalysis
from mixsync.db.models.work import Job, Request
from mixsync.discovery import smart
from mixsync.discovery.radio import MAX_PER_ALBUM, MAX_PER_ARTIST, REFUSED, SEED_GONE, Radio
from mixsync.library.analysis import FEATURE_VERSION

from .conftest import Env, login
from .test_discovery_playlists import Rig, rec


class RadioRig:
    def __init__(self, env: Env) -> None:
        self.base = Rig(env)
        self.env, self.lib = env, self.base.lib
        self.radio = Radio(self.base.disc)
        self.base.link(env.user_id, ("navidrome",))

    def own(
        self,
        n: int,
        vector: list[float] | None,
        *,
        artist: str | None = None,
        group: str | None = None,
        album_artist: str | None = None,
        version: int = FEATURE_VERSION,
        failure: str | None = None,
        analyzed: bool = True,
        listed: bool = True,
        mbid: str | None = None,
    ) -> None:
        """Owned track n (id n) of recording rec(n), listed by the media server as song s{n},
        with an analysis."""
        now, mbid = self.env.clock.now(), mbid or rec(n)
        with self.env.sessions.begin() as s:
            s.add(
                Track(
                    id=n,
                    path=f"Artist/Album/{n:02d} Title {n}.flac",
                    recording_mbid=mbid,
                    release_group_mbid=group or f"group-{n}",
                    artist_mbids=[artist or f"artist-{n}"],
                    album_artist=album_artist or f"Album artist {n}",
                    album=f"Album {n}",
                    status="verified",
                    batch_id="b",
                    imported_at=now,
                )
            )
            s.flush()
            if analyzed:
                s.add(
                    TrackAnalysis(
                        track_id=n,
                        analyzer="test",
                        feature_version=version,
                        vector=vector,
                        analyzed_at=now,
                        failure=failure,
                    )
                )
        if listed:
            song = LibrarySong(
                f"s{n}", mbid, "Artist", f"Title {n}", frozenset(), None, False, None
            )
            self.lib.songs.append(song)

    def create(self, kind: str, ref: str, **kw: Any) -> int:
        with self.env.sessions.begin() as s:
            pl = RadioPlaylist(
                user_id=self.env.user_id, name="Mix", seed_kind=kind, seed_ref=ref, entries=[]
            )
            for k, v in kw.items():
                setattr(pl, k, v)
            s.add(pl)
            s.flush()
            return pl.id

    def queued(self, kind: JobKind = JobKind.RADIO_BUILD) -> list[Job]:
        return [j for j in self.env.jobs(kind) if j.status == "queued"]

    async def build(self, pid: int) -> None:
        """Run a build of playlist pid, as the worker would."""
        with self.env.sessions.begin() as s:
            j = Job(kind=JobKind.RADIO_BUILD, payload={"playlist_id": pid})
            j.run_after = self.env.clock.now()
            s.add(j)
            s.flush()
            j.status, jid = JobState.SUCCEEDED, j.id
        await self.radio.build(self.env.queue.get(jid))

    def pl(self, pid: int) -> RadioPlaylist:
        with self.env.sessions() as s:
            pl = s.get(RadioPlaylist, pid)
            assert pl
            s.expunge(pl)
            return pl

    def songs(self) -> list[str]:
        (songs,) = self.lib.playlists.values()
        return songs


@pytest.fixture
def rr(env: Env) -> RadioRig:
    return RadioRig(env)


def layout(rr: RadioRig) -> None:
    """Seeds 1 and 2 sound the same; the rest lie further away, one step each."""
    for n in (1, 2):
        rr.own(n, [0.0, 0.0], artist="seed", group="G", album_artist="Seed Artist")
    for n, x in ((3, 1.0), (4, 2.0), (5, 3.0), (6, 4.0)):  # one artist: the 4th is capped
        rr.own(n, [x, 0.0], artist="a")
    for n, x in ((7, 5.0), (8, 6.0), (9, 7.0)):  # one album: the 3rd is capped
        rr.own(n, [x, 0.0], group="gb")
    rr.own(10, [8.0, 0.0])
    rr.own(11, None, analyzed=False)  # never analyzed
    rr.own(12, [0.5, 0.0], version=FEATURE_VERSION - 1)  # another analyzer version
    rr.own(13, [0.5, 0.0], failure="decode error")
    rr.own(14, None)  # analyzed without a vector (no bliss)


def seed(rr: RadioRig, case: str) -> tuple[str, str]:
    """(seed kind, seed ref) naming tracks 1 and 2, or track 1 alone for a track seed."""
    if case == "track":
        return "track", rec(1)
    if case == "album":
        return "album", "G"
    if case == "artist":
        return "artist", "Seed Artist"
    entries = [{"recording_mbid": rec(n), "artist": "A", "title": "T"} for n in (1, 2)]
    with rr.env.sessions.begin() as s:
        uid = rr.env.user_id
        pl: SmartPlaylist | DiscoveryPlaylist | MirroredPlaylist
        if case == "smart":
            pl = SmartPlaylist(user_id=uid, name="Seeds", entries=entries)
        elif case == "discovery":
            pl = DiscoveryPlaylist(user_id=uid, name="Seeds", entries=entries)
        else:
            pl = MirroredPlaylist(
                user_id=uid, name="Seeds", upstream_kind="loves", upstream_ref="u", entries=entries
            )
        s.add(pl)
        s.flush()
        return "playlist", f"{case}:{pl.id}"


@pytest.mark.parametrize("case", ["track", "album", "artist", "smart", "discovery", "mirrored"])
async def test_each_seed_kind_builds_the_nearest_owned_analyzed_tracks(
    rr: RadioRig, case: str
) -> None:
    layout(rr)
    assert (MAX_PER_ARTIST, MAX_PER_ALBUM) == (3, 2)
    near = [3, 4, 5, 7, 8, 10]  # 6: a 4th track by artist a; 9: a 3rd from album gb
    want = ([1, 2] if case == "track" else []) + near  # the seed track leads a track seed only
    pid = rr.create(*seed(rr, case))
    await rr.build(pid)
    assert rr.songs() == [f"s{n}" for n in want]
    assert [e["recording_mbid"] for e in rr.pl(pid).entries] == [rec(n) for n in want]
    with rr.env.sessions.begin() as s:  # the size caps it, nearest first
        pl = s.get(RadioPlaylist, pid)
        assert pl
        pl.size = 3
    await rr.build(pid)
    assert rr.songs() == [f"s{n}" for n in want[:3]]
    with rr.env.sessions() as s:
        assert s.scalars(select(Request)).first() is None  # never a request


async def test_a_track_seed_the_media_server_does_not_list_has_no_lead(rr: RadioRig) -> None:
    rr.own(1, [0.0, 0.0], listed=False)
    rr.own(2, [1.0, 0.0])
    pid = rr.create("track", rec(1))
    await rr.build(pid)
    assert rr.songs() == ["s2"]
    assert [e["reason"] for e in rr.pl(pid).entries] == ["sounds close to the seed (0.50)"]


async def test_each_entry_has_a_reason_with_real_values(rr: RadioRig) -> None:
    rr.own(1, [0.0, 0.0], group="G")
    rr.own(2, [2.0, 0.0], group="G")
    rr.own(3, [3.0, 0.0])  # 2 from the centre (1, 0): 1 / (1 + 2); nearest seed track 2
    rr.own(4, [-0.5, 0.0])  # 1.5 from the centre: 0.40; nearest seed track 1
    pid = rr.create("album", "G")
    await rr.build(pid)
    assert [e["reason"] for e in rr.pl(pid).entries] == [
        "sounds close to the seed (0.40); nearest seed track: Artist - Title 1",
        "sounds close to the seed (0.33); nearest seed track: Artist - Title 2",
    ]
    pid = rr.create("track", rec(1), name="One")
    await rr.build(pid)
    assert [e["reason"] for e in rr.pl(pid).entries] == [
        "the seed track",
        "sounds close to the seed (0.67)",  # 4: 0.5 away
        "sounds close to the seed (0.33)",  # 2
        "sounds close to the seed (0.25)",  # 3
    ]


async def test_rebuild_cycle_and_push_match_discovery(rr: RadioRig) -> None:
    rr.own(1, [0.0, 0.0])
    rr.own(2, [1.0, 0.0])
    pid = rr.create("track", rec(1))
    assert rr.pl(pid).cadence_days == 7 and rr.pl(pid).size == 50  # Discovery's defaults
    await rr.build(pid)
    (nxt,) = rr.queued()
    due = rr.env.clock.now() + timedelta(days=7)  # Mid() jitter is 0
    assert nxt.run_after.replace(tzinfo=None) == due.replace(tzinfo=None)
    assert list(rr.lib.names.values()) == ["MixSync: Mix"]
    rr.own(3, [0.5, 0.0])
    rr.radio.d.rng = lambda a, b: 1.0  # the most jitter: 10% of the cadence, at most 6 hours
    with rr.env.sessions.begin() as s:
        s.execute(delete(Job))
    await rr.build(pid)
    (nxt,) = rr.queued()
    due = rr.env.clock.now() + timedelta(days=7, hours=6)
    assert nxt.run_after.replace(tzinfo=None) == due.replace(tzinfo=None)
    assert rr.songs() == ["s1", "s3", "s2"]
    assert len(rr.lib.playlists) == 1  # updated in place, not a second playlist
    assert rr.pl(pid).server_playlist_id == "pl1"


async def test_smart_and_radio_playlists_with_one_id_keep_their_own_builds(rr: RadioRig) -> None:
    rr.own(1, [0.0, 0.0])
    rr.own(2, [1.0, 0.0])
    now = rr.env.clock.now()
    with rr.env.sessions.begin() as s:
        sp = SmartPlaylist(user_id=rr.env.user_id, name="Mix", entries=[])
        rp = RadioPlaylist(user_id=rr.env.user_id, name="Mix", seed_kind="track", seed_ref=rec(1))
        rp.entries = []
        s.add_all([sp, rp])
        s.flush()
        assert sp.id == rp.id
        sp.next_build_at = rp.next_build_at = now + timedelta(days=1)
        smart.ensure_build(s, now, sp)
        smart.ensure_build(s, now, rp)  # the smart build waiting for the same id is not its own
        pid = rp.id
    assert len(rr.queued(JobKind.SMART_BUILD)) == len(rr.queued()) == 1
    with rr.env.sessions.begin() as s:
        rp = s.get(RadioPlaylist, pid)
        assert rp and smart.build_now(s, now, rp)
    (smart_job,) = rr.queued(JobKind.SMART_BUILD)
    (radio_job,) = rr.queued()
    assert smart_job.run_after.replace(tzinfo=None) == (now + timedelta(days=1)).replace(
        tzinfo=None
    )
    assert radio_job.run_after.replace(tzinfo=None) == now.replace(tzinfo=None)
    with rr.env.sessions.begin() as s:
        s.execute(delete(Job).where(Job.id == radio_job.id))
    await rr.build(pid)  # schedules its next build though a smart one waits for the same id
    assert len(rr.queued(JobKind.SMART_BUILD)) == len(rr.queued()) == 1
    assert rr.songs() == ["s1", "s2"]


async def test_a_seed_without_an_analyzed_track_keeps_the_last_playlist(rr: RadioRig) -> None:
    rr.own(1, [0.0, 0.0])
    rr.own(2, [1.0, 0.0])
    pid = rr.create("track", rec(1))
    await rr.build(pid)
    with rr.env.sessions.begin() as s:
        s.execute(delete(TrackAnalysis).where(TrackAnalysis.track_id == 1))
    await rr.build(pid)
    assert rr.pl(pid).last_error == SEED_GONE
    assert rr.songs() == ["s1", "s2"]
    assert len(rr.pl(pid).entries) == 2


async def test_a_trashed_seed_track_is_not_followed_to_its_reused_id(rr: RadioRig) -> None:
    rr.own(1, [0.0, 0.0])
    rr.own(2, [1.0, 0.0])
    pid = rr.create("track", rec(1))
    await rr.build(pid)
    with rr.env.sessions.begin() as s:  # trashed; SQLite hands id 1 to the next import
        s.execute(delete(TrackAnalysis).where(TrackAnalysis.track_id == 1))
        s.execute(delete(Track).where(Track.id == 1))
    rr.own(1, [5.0, 0.0], mbid=rec(50))
    await rr.build(pid)
    assert rr.pl(pid).last_error == SEED_GONE
    assert [e["recording_mbid"] for e in rr.pl(pid).entries] == [rec(1), rec(2)]


async def test_a_deleted_seed_playlist_id_is_never_given_to_a_new_one(rr: RadioRig) -> None:
    layout(rr)
    _, ref = seed(rr, "smart")
    pid = rr.create("playlist", ref)
    await rr.build(pid)
    with rr.env.sessions.begin() as s:  # the newest playlist: without AUTOINCREMENT its id returns
        s.execute(delete(SmartPlaylist))
        new = SmartPlaylist(
            user_id=rr.env.user_id,
            name="Other",
            entries=[{"recording_mbid": rec(10), "artist": "A", "title": "T"}],
        )
        s.add(new)
        s.flush()
        assert f"smart:{new.id}" != ref
    await rr.build(pid)
    assert rr.pl(pid).last_error == SEED_GONE


def test_a_media_server_switch_clears_radio_playlist_ids(rr: RadioRig) -> None:
    pid = rr.create("track", rec(1), server_playlist_id="pl9")
    with rr.env.sessions.begin() as s:
        switch_media_server(s, "navidrome")
    assert rr.pl(pid).server_playlist_id is None


def test_web_refuses_a_seed_without_an_owned_analyzed_track(rr: RadioRig, web: TestClient) -> None:
    rr.own(1, None, group="G", analyzed=False)
    rr.own(2, [0.0, 0.0], version=FEATURE_VERSION - 1, album_artist="Old")
    rr.own(3, [1.0, 0.0])
    with rr.env.sessions.begin() as s:
        bob = create_user(s, "bob", "pw").id
        theirs = SmartPlaylist(
            user_id=bob, name="Bob's", entries=[{"recording_mbid": rec(3), "artist": "A"}]
        )
        s.add(theirs)
        s.flush()
        theirs_ref = f"smart:{theirs.id}"
    tok = login(web, "alice")
    form = {"csrf_token": tok, "name": "Radio: x"}
    for kind, ref in (
        ("album", "G"),  # owned, not analyzed
        ("artist", "Old"),  # analyzed at another version only
        ("track", rec(99)),  # not owned
        ("track", "3"),  # a row id is no track seed
        ("artist", "Nobody"),
        ("playlist", theirs_ref),  # another user's playlist, though its track is analyzed
        ("playlist", "smart:99999999999"),
        ("genre", "rock"),
    ):
        r = web.post("/radio", data=form | {"seed_kind": kind, "seed_ref": ref})
        assert r.status_code == 400, (kind, ref)
        assert REFUSED in r.text
    assert rr.env.jobs(JobKind.RADIO_BUILD) == []


def test_web_create_list_edit_rebuild_now_and_owner_only(rr: RadioRig, web: TestClient) -> None:
    rr.own(1, [0.0, 0.0], group="G", album_artist="Seed Artist")
    tok = login(web, "alice")
    page = web.get("/library/album?kind=verified&group=G&albumartist=Seed+Artist").text
    for label in ("Radio from this album", "Radio from this artist", "Radio from this track"):
        assert label in page
    assert f'name="seed_ref" value="{rec(1)}"' in page  # a track seed is its recording
    with rr.env.sessions.begin() as s:
        s.add(SmartPlaylist(user_id=rr.env.user_id, name="Mine", entries=[]))
    assert "Radio from this playlist" in web.get("/smart").text
    form = {"csrf_token": tok, "seed_kind": "album", "seed_ref": "G", "name": "Radio: Album 1"}
    for bad in ({"name": ""}, {"name": "x" * 201}):
        assert web.post("/radio", data=form | bad).status_code == 400, bad
    assert web.post("/radio", data={"csrf_token": tok, "seed_kind": "album"}).status_code == 400
    assert web.post("/radio", data=form).status_code == 303
    assert web.post("/radio", data=form).status_code == 409  # one name per user
    with rr.env.sessions() as s:
        pl = s.scalars(select(RadioPlaylist)).one()
        pid = pl.id
        assert (pl.seed_kind, pl.seed_ref, pl.size, pl.cadence_days) == ("album", "G", 50, 7)
    assert len(rr.env.jobs(JobKind.RADIO_BUILD)) == 1
    assert "Radio: Album 1" in web.get("/radio").text
    edit = {"csrf_token": tok, "name": "Calm", "size": "20", "cadence_days": "3"}
    for bad in ({"size": "0"}, {"cadence_days": "1.5"}, {"name": "x" * 201}):
        assert web.post(f"/radio/{pid}/settings", data=edit | bad).status_code == 400, bad
    assert (
        web.post(f"/radio/{pid}/settings", data={"csrf_token": tok, "name": "C"}).status_code == 400
    )
    assert web.post(f"/radio/{pid}/settings", data=edit).status_code == 303
    pl = rr.pl(pid)
    assert (pl.name, pl.size, pl.cadence_days) == ("Calm", 20, 3)
    assert web.post(f"/radio/{pid}/build", data={"csrf_token": tok}).status_code == 303
    assert len(rr.env.jobs(JobKind.RADIO_BUILD)) == 1  # moved, not doubled
    with rr.env.sessions.begin() as s:
        p = s.get(RadioPlaylist, pid)
        assert p
        p.last_built_at = rr.env.clock.now()
    assert web.post(f"/radio/{pid}/build", data={"csrf_token": tok}).status_code == 409
    with rr.env.sessions.begin() as s:
        create_user(s, "bob", "pw")
    tok = login(web, "bob")
    assert "Calm" not in web.get("/radio").text
    for path, data in (("settings", edit), ("build", {}), ("delete", {})):
        assert web.post(f"/radio/{pid}/{path}", data={**data, "csrf_token": tok}).status_code == 403
    tok = login(web, "alice")
    assert web.post(f"/radio/{pid}/delete", data={"csrf_token": tok}).status_code == 303
    with rr.env.sessions() as s:
        assert s.get(RadioPlaylist, pid) is None
