"""Radio: nearest owned, analyzed tracks per seed kind with artist and album caps, the refusal,
the reasons, the rebuild cycle and push shared with smart playlists, and the web pages.
Analyses are hand-made rows; nothing here runs bliss."""

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from mixsync.core.jobs import JobKind, JobState
from mixsync.core.types import LibrarySong
from mixsync.db.auth import create_user
from mixsync.db.models.discovery import RadioPlaylist, SmartPlaylist
from mixsync.db.models.library import Track, TrackAnalysis
from mixsync.db.models.work import Job, Request
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
    ) -> None:
        """Owned track n (id n), listed by the media server as song s{n}, with an analysis."""
        now = self.env.clock.now()
        with self.env.sessions.begin() as s:
            s.add(
                Track(
                    id=n,
                    path=f"Artist/Album/{n:02d} Title {n}.flac",
                    recording_mbid=rec(n),
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
        self.lib.songs.append(
            LibrarySong(f"s{n}", rec(n), "Artist", f"Title {n}", frozenset(), None, False, None)
        )

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

    def queued(self) -> list[Job]:
        return [j for j in self.env.jobs(JobKind.RADIO_BUILD) if j.status == "queued"]

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


def seed_ref(rr: RadioRig, kind: str) -> str:
    if kind == "track":
        return "1"
    if kind == "album":
        return "G"
    if kind == "artist":
        return "Seed Artist"
    with rr.env.sessions.begin() as s:
        pl = SmartPlaylist(
            user_id=rr.env.user_id,
            name="Seeds",
            entries=[{"recording_mbid": rec(n), "artist": "A", "title": "T"} for n in (1, 2)],
        )
        s.add(pl)
        s.flush()
        return f"smart:{pl.id}"


@pytest.mark.parametrize("kind", ["track", "album", "artist", "playlist"])
async def test_each_seed_kind_builds_the_nearest_owned_analyzed_tracks(
    rr: RadioRig, kind: str
) -> None:
    layout(rr)
    assert (MAX_PER_ARTIST, MAX_PER_ALBUM) == (3, 2)
    near = [3, 4, 5, 7, 8, 10]  # 6: a 4th track by artist a; 9: a 3rd from album gb
    want = ([1, 2] if kind == "track" else []) + near  # the seed track leads a track seed only
    pid = rr.create(kind, seed_ref(rr, kind))
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
    pid = rr.create("track", "1", name="One")
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
    pid = rr.create("track", "1")
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


async def test_a_seed_without_an_analyzed_track_keeps_the_last_playlist(rr: RadioRig) -> None:
    rr.own(1, [0.0, 0.0])
    rr.own(2, [1.0, 0.0])
    pid = rr.create("track", "1")
    await rr.build(pid)
    with rr.env.sessions.begin() as s:
        s.execute(delete(TrackAnalysis).where(TrackAnalysis.track_id == 1))
    await rr.build(pid)
    assert rr.pl(pid).last_error == SEED_GONE
    assert rr.songs() == ["s1", "s2"]
    assert len(rr.pl(pid).entries) == 2


def test_web_refuses_a_seed_without_an_owned_analyzed_track(rr: RadioRig, web: TestClient) -> None:
    rr.own(1, None, group="G", analyzed=False)
    rr.own(2, [0.0, 0.0], version=FEATURE_VERSION - 1, album_artist="Old")
    tok = login(web, "alice")
    form = {"csrf_token": tok, "name": "Radio: x"}
    for kind, ref in (
        ("album", "G"),  # owned, not analyzed
        ("artist", "Old"),  # analyzed at another version only
        ("track", "99"),  # not owned
        ("track", "99999999999"),
        ("artist", "Nobody"),
        ("playlist", "smart:1"),
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
    with rr.env.sessions.begin() as s:
        s.add(SmartPlaylist(user_id=rr.env.user_id, name="Mine", entries=[]))
    assert "Radio from this playlist" in web.get("/smart").text
    form = {"csrf_token": tok, "seed_kind": "album", "seed_ref": "G", "name": "Radio: Album 1"}
    assert web.post("/radio", data=form).status_code == 303
    assert web.post("/radio", data=form).status_code == 409  # one name per user
    with rr.env.sessions() as s:
        pl = s.scalars(select(RadioPlaylist)).one()
        pid = pl.id
        assert (pl.seed_kind, pl.seed_ref, pl.size, pl.cadence_days) == ("album", "G", 50, 7)
    assert len(rr.env.jobs(JobKind.RADIO_BUILD)) == 1
    assert "Radio: Album 1" in web.get("/radio").text
    edit = {"csrf_token": tok, "name": "Calm", "size": "20", "cadence_days": "3"}
    assert web.post(f"/radio/{pid}/settings", data=edit | {"size": "0"}).status_code == 400
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
