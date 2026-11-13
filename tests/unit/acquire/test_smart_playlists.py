"""Smart playlists: each rule, AND, cap and sort, the cadence chain, the push, never a request,
and the web form's validation."""

from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mixsync.core.errors import PermanentError
from mixsync.core.jobs import JobKind, JobState
from mixsync.core.types import LibrarySong
from mixsync.db.auth import create_user
from mixsync.db.models.discovery import SmartPlaylist
from mixsync.db.models.library import Track
from mixsync.db.models.work import Job, Request
from mixsync.discovery import smart
from mixsync.discovery.smart import NONE_FOUND, Smart

from .conftest import Env, login
from .test_discovery_playlists import MAIN_FOLDER, Rig, rec


class SmartRig:
    def __init__(self, env: Env) -> None:
        self.base = Rig(env)
        self.env, self.lib = env, self.base.lib
        self.smart = Smart(self.base.disc)
        self.base.link(env.user_id, ("navidrome",))

    def own(
        self, n: int, *, year: str | None = "2000", days_ago: int = 0, **song: Any
    ) -> LibrarySong:
        """An owned track n that Navidrome lists as song s{n}."""
        with self.env.sessions.begin() as s:
            s.add(
                Track(
                    path=f"t{n}.mp3",
                    recording_mbid=rec(n),
                    status="verified",
                    batch_id="b",
                    imported_at=self.env.clock.now() - timedelta(days=days_ago),
                    year=year,
                )
            )
        return self.listed(n, **song)

    def listed(
        self,
        n: int,
        genre: str = "Rock",
        nd_year: int | None = None,
        starred: bool = False,
        played: datetime | None = None,
    ) -> LibrarySong:
        song = LibrarySong(
            f"s{n}",
            rec(n),
            "Artist",
            f"Title {n}",
            frozenset({genre.casefold()}),
            nd_year,
            starred,
            played,
        )
        self.lib.songs.append(song)
        return song

    def create(self, rules: dict[str, Any] | None = None, **kw: Any) -> int:
        with self.env.sessions.begin() as s:
            pl = SmartPlaylist(user_id=self.env.user_id, name="Mix", rules=rules or {}, entries=[])
            for k, v in kw.items():
                setattr(pl, k, v)
            s.add(pl)
            s.flush()
            smart.ensure_build(s, self.env.clock.now(), pl)
            return pl.id

    async def build(self) -> None:
        """Run the oldest queued build, as the worker would."""
        with self.env.sessions.begin() as s:
            j = s.scalars(
                select(Job)
                .where(Job.kind == JobKind.SMART_BUILD, Job.status == "queued")
                .order_by(Job.id)
            ).first()
            assert j
            j.status, jid = JobState.SUCCEEDED, j.id
        await self.smart.build(self.env.queue.get(jid))

    def pl(self, pid: int) -> SmartPlaylist:
        with self.env.sessions() as s:
            pl = s.get(SmartPlaylist, pid)
            assert pl
            s.expunge(pl)
            return pl

    def songs(self) -> list[str]:
        (songs,) = self.lib.playlists.values()
        return songs

    async def picks(self, rules: dict[str, Any], **kw: Any) -> list[str]:
        """Build a fresh playlist with `rules`; the song ids pushed."""
        self.lib.playlists.clear()
        with self.env.sessions.begin() as s:
            for pl in s.scalars(select(SmartPlaylist)):
                s.delete(pl)
            # the last build re-queued itself; on Postgres ids aren't reused, so that job
            # would point at the deleted playlist and build() would run it instead
            for j in s.scalars(select(Job).where(Job.kind == JobKind.SMART_BUILD)):
                s.delete(j)
        self.create(rules, **kw)
        await self.build()
        return self.songs() if self.lib.playlists else []


@pytest.fixture
def sr(env: Env) -> SmartRig:
    return SmartRig(env)


async def test_each_rule_filters_and_rules_combine_with_and(sr: SmartRig) -> None:
    now = sr.env.clock.now()
    sr.own(1, genre="Jazz", year="1965")
    sr.own(2, genre="Rock", year="1999", days_ago=40)
    sr.own(3, year=None, nd_year=1985, starred=True, played=now - timedelta(days=2))
    sr.own(4, genre="Rock", year="2010", played=now - timedelta(days=100))
    assert sorted(await sr.picks({})) == ["s1", "s2", "s3", "s4"]
    assert sorted(await sr.picks({"genre": "jazz"})) == ["s1"]  # case-insensitive
    assert sorted(await sr.picks({"year_from": 1980})) == ["s2", "s3", "s4"]  # 3: Navidrome's year
    assert sorted(await sr.picks({"year_from": 1980, "year_to": 1999})) == ["s2", "s3"]
    assert sorted(await sr.picks({"added_days": 30})) == ["s1", "s3", "s4"]
    assert sorted(await sr.picks({"starred": True})) == ["s3"]
    assert sorted(await sr.picks({"unplayed_days": 30})) == [
        "s1",
        "s2",
        "s4",
    ]  # never played counts
    assert sorted(await sr.picks({"genre": "Rock", "added_days": 30, "unplayed_days": 30})) == [
        "s4"
    ]
    assert sorted(await sr.picks({"genre": "Rock", "starred": True, "unplayed_days": 7})) == []


async def test_only_owned_tracks_and_never_a_request(sr: SmartRig) -> None:
    sr.own(1)
    sr.listed(2, starred=True)  # in Navidrome (a discover copy, say) but not owned
    sr.create()
    await sr.build()
    assert sr.songs() == ["s1"]
    assert await sr.picks({"starred": True}) == []
    with sr.env.sessions() as s:
        assert s.scalars(select(Request)).first() is None
        assert {j.kind for j in s.scalars(select(Job))} == {JobKind.SMART_BUILD}
    assert sr.lib.listed[-1] == MAIN_FOLDER  # the main library only


async def test_cap_and_each_sort(sr: SmartRig) -> None:
    sr.own(1, year="1990", days_ago=3)
    sr.own(2, year=None, days_ago=1)
    sr.own(3, year="1970", days_ago=2)
    sr.own(4, year="2005", days_ago=4)
    assert await sr.picks({}, sort="added", size=3) == ["s2", "s3", "s1"]
    assert await sr.picks({}, sort="year") == ["s3", "s1", "s4", "s2"]  # no year: last
    order = iter([0.1, 0.4, 0.3, 0.2])  # t4, t1, t3, t2: oldest import first
    sr.smart.d.rng = lambda a, b: next(order, 0.0)
    assert await sr.picks({}, sort="random", size=2) == ["s4", "s2"]


async def test_rebuild_reschedules_on_its_cadence(sr: SmartRig) -> None:
    sr.own(1)
    pid = sr.create(cadence_days=3)
    sr.smart.start_chains()  # already queued: no second build
    assert len(sr.env.jobs(JobKind.SMART_BUILD)) == 1
    await sr.build()
    queued = [j for j in sr.env.jobs(JobKind.SMART_BUILD) if j.status == "queued"]
    assert len(queued) == 1
    due = sr.env.clock.now() + timedelta(days=3)  # Mid() jitter is 0
    assert queued[0].run_after.replace(tzinfo=None) == due.replace(tzinfo=None)
    assert sr.pl(pid).last_built_at is not None
    sr.lib.error = PermanentError("bad password")  # a failed build keeps the chain too
    sr.env.clock.advance(timedelta(days=3))
    await sr.build()
    assert sr.pl(pid).last_error == "Navidrome: bad password"
    assert len([j for j in sr.env.jobs(JobKind.SMART_BUILD) if j.status == "queued"]) == 1


async def test_a_rebuild_replaces_the_navidrome_copy(sr: SmartRig) -> None:
    sr.own(1)
    sr.own(2, days_ago=40)
    pid = sr.create({"added_days": 30})
    await sr.build()
    assert sr.songs() == ["s1"]
    assert list(sr.lib.names.values()) == ["MixSync: Mix"]
    sr.env.clock.advance(timedelta(days=31))
    sr.own(3)
    await sr.build()
    assert sr.songs() == ["s3"]  # 1 aged out, 3 joined
    assert len(sr.lib.playlists) == 1  # replaced, not a second playlist
    assert sr.pl(pid).server_playlist_id == "pl1"
    assert [e["title"] for e in sr.pl(pid).entries] == ["Title 3"]


async def test_no_push_when_navidrome_lists_none_of_the_owned_tracks(sr: SmartRig) -> None:
    sr.own(1)
    sr.lib.songs.clear()  # not scanned yet
    pid = sr.create()
    await sr.build()
    assert sr.lib.playlists == {}
    assert sr.pl(pid).last_error == NONE_FOUND


def test_web_create_validates_and_queues_a_build(sr: SmartRig, web: TestClient) -> None:
    tok = login(web, "alice")
    good = {"csrf_token": tok, "name": "Old rock", "size": "50", "cadence_days": "1"}
    for bad in (
        {"name": ""},
        {"size": "0"},
        {"size": "-1"},
        {"cadence_days": "0"},
        {"cadence_days": "1.5"},
        {"added_days": "-1"},
        {"unplayed_days": "abc"},
        {"year_from": "2000", "year_to": "1990"},
        {"sort": "loudness"},
    ):
        assert web.post("/smart", data={**good, **bad}).status_code == 400, bad
    form = {**good, "genre": " Rock ", "year_from": "1970", "year_to": "1979"}
    form |= {"added_days": "0", "starred": "true", "sort": "year"}
    assert web.post("/smart", data=form).status_code == 303
    with sr.env.sessions() as s:
        pl = s.scalars(select(SmartPlaylist)).one()
        assert pl.rules == {
            "genre": "Rock",
            "year_from": 1970,
            "year_to": 1979,
            "added_days": 0,
            "starred": True,
        }
        assert (pl.sort, pl.size, pl.cadence_days) == ("year", 50, 1)
    assert len(sr.env.jobs(JobKind.SMART_BUILD)) == 1
    assert web.post("/smart", data=good | {"name": "Old rock"}).status_code == 409
    assert "Old rock" in web.get("/smart").text


def test_web_edit_rebuild_now_and_owner_only(sr: SmartRig, web: TestClient) -> None:
    pid = sr.create({"genre": "Rock"})
    tok = login(web, "alice")
    edit = {"csrf_token": tok, "name": "Mix", "size": "10", "cadence_days": "7"}
    assert web.post(f"/smart/{pid}/settings", data=edit | {"year_to": "x"}).status_code == 400
    assert (
        web.post(f"/smart/{pid}/settings", data=edit | {"unplayed_days": "90"}).status_code == 303
    )
    pl = sr.pl(pid)
    assert (pl.rules, pl.size, pl.cadence_days) == ({"unplayed_days": 90}, 10, 7)
    assert web.post(f"/smart/{pid}/build", data={"csrf_token": tok}).status_code == 303
    assert len(sr.env.jobs(JobKind.SMART_BUILD)) == 1  # moved, not doubled
    with sr.env.sessions.begin() as s:
        p = s.get(SmartPlaylist, pid)
        assert p
        p.last_built_at = sr.env.clock.now()
    assert web.post(f"/smart/{pid}/build", data={"csrf_token": tok}).status_code == 409
    with sr.env.sessions.begin() as s:
        create_user(s, "bob", "pw")
    tok = login(web, "bob")
    assert "<h2>Mix</h2>" not in web.get("/smart").text
    for path, data in (("settings", edit), ("build", {}), ("delete", {})):
        assert web.post(f"/smart/{pid}/{path}", data={**data, "csrf_token": tok}).status_code == 403
    tok = login(web, "alice")
    assert web.post(f"/smart/{pid}/delete", data={"csrf_token": tok}).status_code == 303
    with sr.env.sessions() as s:
        assert s.get(SmartPlaylist, pid) is None
