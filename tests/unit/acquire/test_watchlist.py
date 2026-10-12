"""Watchlist: cadence, the poll (filters, dedupe, cap, fan-out), the MB search and the UI."""

import asyncio
import json
import re
from collections.abc import Callable
from datetime import UTC, date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from mixsync.core.jobs import JobKind, JobState
from mixsync.core.types import EntityRef, ReleaseGroupHit, ReleaseRef
from mixsync.db.auth import create_user
from mixsync.db.engine import user_sessions
from mixsync.db.models.library import Track
from mixsync.db.models.watchlist import Follow, FollowHit, WatchedEntity
from mixsync.db.models.work import Job, Request
from mixsync.db.queue import enqueue_in
from mixsync.metadata.musicbrainz import MusicBrainzProvider
from mixsync.ratelimit.client import PoliteClient, polite_client
from mixsync.watchlist import follows
from mixsync.watchlist.poller import Watcher, interval_for, jitter

from .conftest import Env, login

ARTIST = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
LABEL = "c595c289-47ce-4fba-b999-b87503e8cb71"


def rid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


class FakeWatch:
    def __init__(self) -> None:
        self.hits: list[ReleaseGroupHit] = []
        self.calls: list[tuple[str, str, str | None]] = []
        self.found: list[EntityRef] = []
        self.truncated = False
        self.browsed: list[str] = []
        self.no_releases = False

    async def new_release_groups(
        self, kind: str, mbid: str, since: str | None
    ) -> list[ReleaseGroupHit]:
        self.calls.append((kind, mbid, since))
        return self.hits

    async def search_entities(self, kind: str, name: str) -> list[EntityRef]:
        return self.found

    async def entity_name(self, kind: str, mbid: str) -> str:
        return "Looked Up"

    async def artist_discography(self, mbid: str) -> tuple[list[ReleaseGroupHit], bool]:
        return self.hits, self.truncated

    async def browse_group_releases(self, release_group_mbid: str) -> list[ReleaseRef]:
        self.browsed.append(release_group_mbid)
        if self.no_releases:
            return []
        n = int(release_group_mbid[-12:])
        return [ReleaseRef(rid(2000 + n), "x", "y", "2020", status="Official")]


def rg(n: int, date: str, type: str = "album") -> ReleaseGroupHit:
    return ReleaseGroupHit(rid(n), f"Album {n}", "Artist", type, date, (rid(1000 + n),))


class Mid:  # rng that picks the middle of the range
    def __call__(self, a: float, b: float) -> float:
        return (a + b) / 2


def today(env: Env) -> str:
    return env.clock.now().date().isoformat()


def follow(env: Env, kind: str = "artist", mbid: str = ARTIST, user: int | None = None) -> int:
    with env.sessions.begin() as s:
        f = follows.add_follow(s, env.clock.now(), user or env.user_id, kind, mbid, "Someone")
        return f.id


def entity_id(env: Env, follow_id: int) -> int:
    with env.sessions() as s:
        f = s.get(Follow, follow_id)
        assert f
        return f.entity_id


async def poll(env: Env, watch: FakeWatch, eid: int) -> None:
    """Run the entity's next queued check, as the worker would."""
    with env.sessions.begin() as s:
        j = s.scalars(
            select(Job).where(Job.kind == JobKind.WATCHLIST_POLL, Job.status == "queued")
        ).first()
        assert j
        j.status = JobState.SUCCEEDED
        jid = j.id
    await Watcher(env.sessions, env.clock, watch, Mid()).poll(env.queue.get(jid))


def history(env: Env, fid: int) -> dict[str, str]:
    with env.sessions() as s:
        rows = s.scalars(select(FollowHit).where(FollowHit.follow_id == fid))
        return {h.title: h.status for h in rows}


def test_interval_buckets() -> None:
    t = date(2026, 6, 1)
    days = lambda d: timedelta(days=d)  # noqa: E731
    assert interval_for("2026-05-30", t) == days(3)
    assert interval_for("2025-06-01", t) == days(3)  # exactly 12 months
    assert interval_for("2025-05-31", t) == days(7)
    assert interval_for("2024-06-01", t) == days(7)
    assert interval_for("2024-05-31", t) == days(14)
    assert interval_for("2019", t) == days(14)
    assert interval_for(None, t) == days(14)


def test_jitter_stays_in_range() -> None:
    for base in (timedelta(days=3), timedelta(days=7), timedelta(days=14)):
        samples = [jitter(base) for _ in range(200)]
        assert all(base / 2 <= j <= base * 1.5 for j in samples)
        assert len(set(samples)) > 100  # random, not a fixed point
    assert jitter(timedelta(days=3)) <= timedelta(days=4.5)  # the roadmap's bound


async def test_new_release_becomes_a_request(env: Env) -> None:
    fid, watch = follow(env), FakeWatch()
    watch.hits = [rg(1, today(env))]
    await poll(env, watch, entity_id(env, fid))
    with env.sessions() as s:
        (req,) = s.scalars(select(Request)).all()
        (hit,) = s.scalars(select(FollowHit)).all()
        e = s.scalars(select(WatchedEntity)).one()
        assert (req.source, req.user_id, req.release_mbid) == ("watchlist", env.user_id, rid(1001))
        assert (hit.status, hit.request_id) == ("requested", req.id)
        assert e.last_release_date == today(env)
        assert (
            s.scalar(select(func.count()).select_from(Job).where(Job.kind == JobKind.SEARCH)) == 1
        )
    # the next check: 3 days (active) at a random point, here the middle
    nxt = [j for j in env.jobs(JobKind.WATCHLIST_POLL) if j.status == "queued" and j.id > 1]
    (job,) = nxt[-1:]
    assert job.run_after.replace(tzinfo=UTC) == env.clock.now() + timedelta(days=3)


async def test_window_overlaps_and_hits_are_not_repeated(env: Env) -> None:
    fid, watch = follow(env), FakeWatch()
    watch.hits = [rg(1, today(env))]
    eid = entity_id(env, fid)
    first = (env.clock.now() - timedelta(days=365)).date().isoformat()
    await poll(env, watch, eid)
    env.clock.advance(timedelta(days=3))
    await poll(env, watch, eid)  # the overlap returns the same release group
    assert watch.calls[0] == ("artist", ARTIST, first)  # first check: a year back, bounded
    last = (env.clock.now() - timedelta(days=3) - timedelta(days=7)).date().isoformat()
    assert watch.calls[1][2] == last
    with env.sessions() as s:
        assert s.scalar(select(func.count()).select_from(Request)) == 1
        assert s.scalar(select(func.count()).select_from(FollowHit)) == 1


async def test_filters(env: Env) -> None:
    fid, watch = follow(env), FakeWatch()
    now = env.clock.now().date()
    with env.sessions.begin() as s:
        f = s.get(Follow, fid)
        assert f
        f.min_date = (now + timedelta(days=1)).isoformat()
    watch.hits = [
        rg(1, (now + timedelta(days=5)).isoformat()),  # passes
        rg(2, (now + timedelta(days=5)).isoformat(), "single"),  # type not followed
        rg(3, today(env)),  # before min_date
        rg(4, (now - timedelta(days=3)).isoformat()),  # inside the overlap, before the follow
        rg(5, (now - timedelta(days=30)).isoformat()),  # older than the follow: not new, no row
    ]
    await poll(env, watch, entity_id(env, fid))
    assert history(env, fid) == {
        "Album 1": "requested",
        "Album 2": "skipped_filter",
        "Album 3": "skipped_filter",
        "Album 4": "skipped_filter",
    }


async def test_owned_and_already_requested_are_skipped(env: Env) -> None:
    fid, watch = follow(env), FakeWatch()
    with env.sessions.begin() as s:
        s.add(
            Track(
                path="a.mp3",
                release_group_mbid=rid(1),
                status="verified",
                batch_id="b",
                imported_at=env.clock.now(),
            )
        )
        s.add(Request(user_id=env.user_id, release_mbid=rid(1002)))  # asked for by hand
    watch.hits = [rg(n, today(env)) for n in (1, 2, 3)]
    await poll(env, watch, entity_id(env, fid))
    assert history(env, fid) == {
        "Album 1": "skipped_owned",
        "Album 2": "skipped_requested",
        "Album 3": "requested",
    }


async def test_label_cap_and_one_click_request(env: Env, web: TestClient) -> None:
    fid, watch = follow(env, "label", LABEL), FakeWatch()
    watch.hits = [rg(n, today(env)) for n in range(1, 8)]
    await poll(env, watch, entity_id(env, fid))
    counts = list(history(env, fid).values())
    assert counts.count("requested") == 5 and counts.count("skipped_cap") == 2  # label default
    with env.sessions() as s:
        capped = s.scalars(select(FollowHit).where(FollowHit.status == "skipped_cap")).first()
        assert capped
        hid = capped.id
    tok = login(web, "alice")
    r = web.post(f"/watchlist/{fid}/hits/{hid}/request", data={"csrf_token": tok})
    assert r.status_code == 303
    with env.sessions() as s:
        hit = s.get(FollowHit, hid)
        assert hit and hit.status == "requested" and hit.request_id
        req = s.get(Request, hit.request_id)
        assert req and req.source == "watchlist"
    again = web.post(f"/watchlist/{fid}/hits/{hid}/request", data={"csrf_token": tok})
    assert again.status_code == 409  # only a capped release can be requested


async def test_artists_are_uncapped_and_cap_window_is_a_week(env: Env) -> None:
    fid, watch = follow(env), FakeWatch()
    watch.hits = [rg(n, today(env)) for n in range(1, 9)]
    await poll(env, watch, entity_id(env, fid))
    assert set(history(env, fid).values()) == {"requested"}
    # a cap of 1 counts only the last 7 days
    with env.sessions.begin() as s:
        f = s.get(Follow, fid)
        assert f
        f.weekly_cap = 1
    env.clock.advance(timedelta(days=8))
    watch.hits = [rg(20, today(env)), rg(21, today(env))]
    await poll(env, watch, entity_id(env, fid))
    assert {k: v for k, v in history(env, fid).items() if k in ("Album 20", "Album 21")} == {
        "Album 20": "requested",
        "Album 21": "skipped_cap",
    }


async def test_two_followers_one_musicbrainz_call(env: Env) -> None:
    with env.sessions.begin() as s:
        bob = create_user(s, "bob", "pw").id
    a, b = follow(env), follow(env, user=bob)
    assert entity_id(env, a) == entity_id(env, b)
    with env.sessions() as s:  # one chain, not one per follower
        assert s.scalar(select(func.count()).select_from(Job)) == 1
    watch = FakeWatch()
    watch.hits = [rg(1, today(env))]
    await poll(env, watch, entity_id(env, a))
    assert len(watch.calls) == 1
    with env.sessions() as s:
        users = sorted(s.scalars(select(Request.user_id)))
        assert users == sorted([env.user_id, bob])


async def test_check_now_moves_the_queued_check_and_is_rate_limited(env: Env) -> None:
    fid, watch = follow(env), FakeWatch()
    eid = entity_id(env, fid)
    await poll(env, watch, eid)
    now = env.clock.now()
    with env.sessions.begin() as s:
        e = s.get(WatchedEntity, eid)
        assert e
        assert not follows.check_now(s, now, e)  # just checked
    env.clock.advance(timedelta(hours=2))
    with env.sessions.begin() as s:
        e = s.get(WatchedEntity, eid)
        assert e
        assert follows.check_now(s, env.clock.now(), e)
    queued = [j for j in env.jobs(JobKind.WATCHLIST_POLL) if j.status == "queued"]
    assert len(queued) == 1 and queued[0].run_after.replace(tzinfo=UTC) == env.clock.now()


# MusicBrainz


Make = Callable[[str, Callable[[httpx.Request], httpx.Response]], PoliteClient]


async def test_artist_search_is_one_date_range_request(make_client: Make) -> None:
    seen: list[httpx.Request] = []
    body = {
        "release-groups": [
            {
                "id": rid(1),
                "title": "New",
                "primary-type": "Album",
                "secondary-types": ["Live"],
                "first-release-date": "2026-05",
                "artist-credit": [{"name": "A", "joinphrase": "", "artist": {"id": ARTIST}}],
                "releases": [{"id": rid(11)}, {"id": rid(12)}],
            },
            {"id": rid(2), "title": "Undated", "primary-type": "EP", "releases": [{"id": rid(21)}]},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=json.dumps(body))

    mb = MusicBrainzProvider(make_client("musicbrainz", handler))
    hits = await mb.new_release_groups("artist", ARTIST, "2026-04-01")
    (req,) = seen
    assert req.url.path == "/ws/2/release-group"
    assert req.url.params["query"] == f"arid:{ARTIST} AND firstreleasedate:[2026-04-01 TO *]"
    assert req.url.params["limit"] == "100"
    assert hits == [ReleaseGroupHit(rid(1), "New", "A", "live", "2026-05", (rid(11), rid(12)))]


async def test_label_search_maps_releases_to_groups(make_client: Make) -> None:
    seen: list[httpx.Request] = []

    def rel(i: int, date: str, group: int, *secondary: str) -> dict[str, object]:
        # the release-group shape a live label search returns (checked 2026-10-11, #43):
        # "secondary-types" is left out when there are none
        rg: dict[str, object] = {"id": rid(group), "primary-type": "Album"}
        if secondary:
            rg["secondary-types"] = list(secondary)
        return {
            "id": rid(i),
            "title": f"R{i}",
            "date": date,
            "artist-credit": [{"name": "A", "artist": {"id": ARTIST}}],
            "release-group": rg,
        }

    body = {
        "releases": [
            rel(1, "2026-06-02", 50),
            rel(2, "2026-05-30", 50),
            rel(3, "2026-06-01", 51, "Compilation"),
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=json.dumps(body))

    mb = MusicBrainzProvider(make_client("musicbrainz", handler))
    hits = await mb.new_release_groups("label", LABEL, None)
    (req,) = seen
    assert req.url.path == "/ws/2/release"
    assert req.url.params["query"] == f"laid:{LABEL}"
    assert [(h.group_id, h.release_ids, h.date, h.type) for h in hits] == [
        (rid(50), (rid(1), rid(2)), "2026-05-30", "album"),
        (rid(51), (rid(3),), "2026-06-01", "compilation"),
    ]


# Web


def test_follow_search_page_and_own_follows_only(env: Env, web: TestClient) -> None:
    watch = FakeWatch()
    watch.found = [EntityRef(ARTIST, "Radiohead", "UK band")]
    web.app.state.watch = watch  # type: ignore[attr-defined]
    with env.sessions.begin() as s:
        create_user(s, "bob", "pw")
    tok = login(web, "alice")
    page = web.get("/watchlist/search", params={"kind": "artist", "q": "radio"})
    assert "Radiohead" in page.text and "UK band" in page.text
    r = web.post(
        "/watchlist/follow",
        data={
            "csrf_token": tok,
            "kind": "artist",
            "mbid": f"https://musicbrainz.org/artist/{ARTIST}",
        },
    )
    assert r.status_code == 303
    fid = int(r.headers["location"].split("/")[2])
    assert r.headers["location"].endswith("/catalog")
    listing = web.get("/watchlist").text
    assert "Looked Up" in listing and "never" in listing
    assert "Nothing found yet" in web.get(f"/watchlist/{fid}").text
    assert web.post(f"/watchlist/{fid}/check", data={"csrf_token": tok}).status_code == 303
    bob = login(web, "bob")
    assert web.get(f"/watchlist/{fid}").status_code == 403
    assert web.post(f"/watchlist/{fid}/delete", data={"csrf_token": bob}).status_code == 403
    tok = login(web, "alice")
    bad = web.post(f"/watchlist/{fid}/filters", data={"csrf_token": tok, "min_date": "soon"})
    assert bad.status_code == 400
    ok = web.post(
        f"/watchlist/{fid}/filters",
        data={
            "csrf_token": tok,
            "types": ["album", "live"],
            "weekly_cap": "3",
            "min_date": "2026-01",
        },
    )
    assert ok.status_code == 303
    with env.sessions() as s:
        f = s.get(Follow, fid)
        assert f
        assert (f.release_types, f.min_date, f.weekly_cap) == (
            ["album", "live"],
            "2026-01",
            3,
        )
    assert web.post(f"/watchlist/{fid}/delete", data={"csrf_token": tok}).status_code == 303
    with env.sessions() as s:
        assert s.scalar(select(func.count()).select_from(Follow)) == 0


def test_a_user_without_request_cannot_follow(web: TestClient) -> None:
    tok = login(web, "nocap")
    assert web.get("/watchlist").status_code == 403
    r = web.post("/watchlist/follow", data={"csrf_token": tok, "kind": "artist", "mbid": ARTIST})
    assert r.status_code == 403


def test_bad_input_is_refused(web: TestClient) -> None:
    web.app.state.watch = FakeWatch()  # type: ignore[attr-defined]
    tok = login(web, "alice")
    for data in ({"kind": "artist", "mbid": "nope"}, {"kind": "album", "mbid": ARTIST}):
        r = web.post("/watchlist/follow", data={"csrf_token": tok, **data})
        assert r.status_code == 400


async def test_failed_check_still_queues_the_next(env: Env) -> None:
    fid = follow(env)

    class Down(FakeWatch):
        async def new_release_groups(
            self, kind: str, mbid: str, since: str | None
        ) -> list[ReleaseGroupHit]:
            raise RuntimeError("MusicBrainz is down")

    with pytest.raises(RuntimeError):
        await poll(env, Down(), entity_id(env, fid))
    live = [j for j in env.jobs(JobKind.WATCHLIST_POLL) if j.status == "queued"]
    assert len(live) == 1 and live[0].run_after.replace(tzinfo=UTC) > env.clock.now()


async def test_rerun_of_a_job_leaves_one_successor(env: Env) -> None:
    import random

    fid = follow(env)
    eid = entity_id(env, fid)
    job = env.jobs(JobKind.WATCHLIST_POLL)[0]
    watcher = Watcher(env.sessions, env.clock, FakeWatch(), random.uniform)
    rec = env.queue.get(job.id)
    await watcher.poll(rec)
    await watcher.poll(rec)  # e.g. the lease expired and the job ran again
    with env.sessions() as s:
        later = s.scalars(
            select(Job).where(Job.kind == JobKind.WATCHLIST_POLL, Job.id != job.id)
        ).all()
        assert len(later) == 1 and later[0].payload == {"entity_id": eid}


async def test_start_chains_restores_a_dead_chain(env: Env) -> None:
    fid = follow(env)
    with env.sessions.begin() as s:
        for j in s.scalars(select(Job)):
            j.status = JobState.FAILED
    Watcher(env.sessions, env.clock, FakeWatch()).start_chains()
    assert len([j for j in env.jobs(JobKind.WATCHLIST_POLL) if j.status == "queued"]) == 1
    assert fid


def _catalog_world(env: Env, web: TestClient) -> tuple[FakeWatch, str, int]:
    watch = FakeWatch()
    watch.hits = [
        ReleaseGroupHit(rid(1), "Old LP", "A", "album", "2001", ()),
        ReleaseGroupHit(rid(2), "New LP", "A", "album", "2020", ()),
        ReleaseGroupHit(rid(3), "Owned LP", "A", "album", "2010", ()),
        ReleaseGroupHit(rid(4), "Asked LP", "A", "album", "2011", ()),
        ReleaseGroupHit(rid(5), "A Single", "A", "single", "2015", ()),
        ReleaseGroupHit(rid(6), "Odd", "A", "other", "", ()),
    ]
    web.app.state.watch = watch  # type: ignore[attr-defined]
    tok = login(web, "alice")
    r = web.post("/watchlist/follow", data={"csrf_token": tok, "kind": "artist", "mbid": ARTIST})
    assert r.status_code == 303
    fid = int(r.headers["location"].split("/")[2])
    with env.sessions.begin() as s:
        s.add(
            Track(
                path="o.mp3",
                release_group_mbid=rid(3),
                status="verified",
                batch_id="b",
                imported_at=env.clock.now(),
            )
        )
        s.add(
            FollowHit(
                follow_id=fid,
                release_group_mbid=rid(4),
                release_mbid=rid(9),
                status="requested",
            )
        )
    return watch, tok, fid


def test_picker_marks_owned_and_requested(env: Env, web: TestClient) -> None:
    _, _, fid = _catalog_world(env, web)
    page = web.get(f"/watchlist/{fid}/catalog").text
    assert page.index("Old LP") > page.index("New LP")  # newest first
    assert page.index("<legend>album") < page.index("<legend>single") < page.index("<legend>other")
    assert re.search(r"<input[^>]* checked", page) is None
    assert page.count('" disabled>') == 2 and "owned" in page and "already requested" in page
    assert web.app.state.watch.browsed == []  # type: ignore[attr-defined]


def test_label_follow_redirects_to_the_follow_page(env: Env, web: TestClient) -> None:
    web.app.state.watch = FakeWatch()  # type: ignore[attr-defined]
    tok = login(web, "alice")
    r = web.post("/watchlist/follow", data={"csrf_token": tok, "kind": "label", "mbid": LABEL})
    assert not r.headers["location"].endswith("/catalog")
    lid = int(r.headers["location"].split("/")[2])
    assert web.get(f"/watchlist/{lid}/catalog").status_code == 404


def _run_pick(env: Env, watch: FakeWatch) -> None:
    (job,) = env.jobs(JobKind.WATCHLIST_PICK)
    asyncio.run(Watcher(env.sessions, env.clock, watch).pick(env.queue.get(job.id)))


def test_picker_queues_and_the_job_requests(env: Env, web: TestClient) -> None:
    watch, tok, fid = _catalog_world(env, web)
    picked = [rid(1), rid(3), rid(4), rid(99)]  # owned, requested and unknown are refused
    r = web.post(f"/watchlist/{fid}/catalog", data={"csrf_token": tok, "groups": picked})
    assert r.status_code == 303 and "done=queued&n=1" in r.headers["location"]
    assert watch.browsed == []  # the web request did no release browse
    with env.sessions() as s:
        assert s.scalar(select(func.count()).select_from(Request)) == 0
        hit = s.scalars(select(FollowHit).where(FollowHit.release_group_mbid == rid(1))).one()
        assert hit.status == "picked"
    assert "picked" in web.get(f"/watchlist/{fid}").text
    _run_pick(env, watch)
    _run_pick(env, watch)  # a re-run adds nothing
    with env.sessions() as s:
        reqs = s.scalars(select(Request)).all()
        assert [(q.release_mbid, q.source, q.user_id) for q in reqs] == [
            (rid(2001), "watchlist", env.user_id)
        ]
        hit = s.scalars(select(FollowHit).where(FollowHit.release_group_mbid == rid(1))).one()
        assert (hit.status, hit.title, hit.request_id) == ("requested", "Old LP", reqs[0].id)
        assert (
            s.scalar(select(func.count()).select_from(Job).where(Job.kind == JobKind.SEARCH)) == 1
        )
    assert watch.browsed == [rid(1)]


def test_pick_skips_a_group_already_requested_by_hand(env: Env, web: TestClient) -> None:
    watch, tok, fid = _catalog_world(env, web)
    with env.sessions.begin() as s:
        s.add(
            Request(user_id=env.user_id, release_mbid=rid(2001))
        )  # the release the pick would use
    web.post(f"/watchlist/{fid}/catalog", data={"csrf_token": tok, "groups": [rid(1)]})
    _run_pick(env, watch)
    with env.sessions() as s:
        assert s.scalar(select(func.count()).select_from(Request)) == 1
        hit = s.scalars(select(FollowHit).where(FollowHit.release_group_mbid == rid(1))).one()
        assert hit.status == "skipped_requested"
    page = web.get(f"/watchlist/{fid}/catalog").text
    assert page.count('" disabled>') == 3  # owned, requested, and now skipped_requested


def test_picker_is_closed_to_other_users(env: Env, web: TestClient) -> None:
    _, _, fid = _catalog_world(env, web)
    with env.sessions.begin() as s:
        create_user(s, "bob", "pw")
    bob = login(web, "bob")
    assert web.get(f"/watchlist/{fid}/catalog").status_code == 403
    r = web.post(f"/watchlist/{fid}/catalog", data={"csrf_token": bob, "groups": [rid(1)]})
    assert r.status_code == 403
    assert env.jobs(JobKind.WATCHLIST_PICK) == []


def test_request_button_refuses_an_owned_album(env: Env, web: TestClient) -> None:
    _, tok, fid = _catalog_world(env, web)
    with env.sessions.begin() as s:
        h = FollowHit(
            follow_id=fid, release_group_mbid=rid(3), release_mbid=rid(7), status="skipped_filter"
        )
        s.add(h)
        s.flush()
        hid = h.id
    r = web.post(f"/watchlist/{fid}/hits/{hid}/request", data={"csrf_token": tok})
    assert r.status_code == 303 and "done=refused" in r.headers["location"]
    with env.sessions() as s:
        assert s.scalar(select(func.count()).select_from(Request)) == 0


def test_mb_routes_work_on_a_real_sqlite_file_with_the_db_bucket(env: Env, web: TestClient) -> None:
    """The login's last_seen write must not hold the lock the rate-limit bucket needs."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/release-group"):
            return httpx.Response(200, json={"release-group-count": 0, "release-groups": []})
        if request.url.path.startswith("/ws/2/artist/"):
            return httpx.Response(200, json={"id": ARTIST, "name": "Real Name"})
        return httpx.Response(
            200, json={"artists": [{"id": ARTIST, "name": "Real Name", "disambiguation": ""}]}
        )

    client = polite_client(
        "musicbrainz", env.settings, env.sessions, transport=httpx.MockTransport(handler)
    )
    web.app.state.watch = MusicBrainzProvider(client)  # type: ignore[attr-defined]
    tok = login(web, "alice")
    assert "Real Name" in web.get("/watchlist/search", params={"kind": "artist", "q": "x"}).text
    r = web.post("/watchlist/follow", data={"csrf_token": tok, "kind": "artist", "mbid": ARTIST})
    assert r.status_code == 303
    fid = r.headers["location"].split("/")[2]
    assert web.get(f"/watchlist/{fid}/catalog").status_code == 200
    assert web.post(f"/watchlist/{fid}/catalog", data={"csrf_token": tok}).status_code == 303


async def test_follow_while_a_check_runs_queues_a_successor(env: Env) -> None:
    follow(env)
    with env.sessions.begin() as s:
        for j in s.scalars(select(Job)):
            j.status = JobState.RUNNING
    with env.sessions.begin() as s:
        bob = create_user(s, "bob", "pw").id
    follow(env, user=bob)
    assert len([j for j in env.jobs(JobKind.WATCHLIST_POLL) if j.status == "queued"]) == 1


async def test_a_new_follow_pulls_a_far_future_check_forward(env: Env) -> None:
    follow(env)
    far = env.clock.now() + timedelta(days=10)
    with env.sessions.begin() as s:
        for j in s.scalars(select(Job)):
            j.run_after = far
        bob = create_user(s, "bob", "pw").id
    follow(env, user=bob)
    (job,) = env.jobs(JobKind.WATCHLIST_POLL)
    assert job.run_after.replace(tzinfo=UTC) == env.clock.now()


async def test_discography_stops_at_the_page_cap(make_client: Make) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        off = int(request.url.params["offset"])
        groups = [
            {"id": rid(off + i + 1), "title": "T", "primary-type": "Album"} for i in range(100)
        ]
        return httpx.Response(
            200, text=json.dumps({"release-group-count": 900, "release-groups": groups})
        )

    mb = MusicBrainzProvider(make_client("musicbrainz", handler))
    found, truncated = await mb.artist_discography(ARTIST)
    assert len(seen) == 5 and len(found) == 500 and truncated
    assert [r.url.params["offset"] for r in seen] == ["0", "100", "200", "300", "400"]
    assert all(r.url.params["artist"] == ARTIST for r in seen)


async def test_short_discography_is_one_page(make_client: Make) -> None:
    body = {"release-group-count": 2, "release-groups": [{"id": rid(1), "title": "T"}]}
    mb = MusicBrainzProvider(make_client("musicbrainz", lambda r: httpx.Response(200, json=body)))
    found, truncated = await mb.artist_discography(ARTIST)
    assert len(found) == 1 and not truncated


def test_start_chains_requeues_a_pick_whose_job_failed(env: Env, web: TestClient) -> None:
    watch, tok, fid = _catalog_world(env, web)
    web.post(f"/watchlist/{fid}/catalog", data={"csrf_token": tok, "groups": [rid(1)]})
    with env.sessions.begin() as s:
        for j in s.scalars(select(Job).where(Job.kind == JobKind.WATCHLIST_PICK)):
            j.status = JobState.FAILED
    Watcher(env.sessions, env.clock, watch).start_chains()
    Watcher(env.sessions, env.clock, watch).start_chains()  # one live job is enough
    assert [j.status for j in env.jobs(JobKind.WATCHLIST_PICK)] == ["failed", "queued"]


def test_pick_dedupes_a_manual_request_for_another_release_of_the_group(
    env: Env, web: TestClient
) -> None:
    watch, tok, fid = _catalog_world(env, web)
    with env.sessions.begin() as s:  # release X of group 1; the pick will resolve to rid(2001)
        s.add(Request(user_id=env.user_id, release_mbid=rid(5000), release_group_mbid=rid(1)))
    web.post(f"/watchlist/{fid}/catalog", data={"csrf_token": tok, "groups": [rid(1)]})
    _run_pick(env, watch)
    with env.sessions() as s:
        assert s.scalar(select(func.count()).select_from(Request)) == 1
        hit = s.scalars(select(FollowHit).where(FollowHit.release_group_mbid == rid(1))).one()
        assert hit.status == "skipped_requested"


def test_request_button_refuses_a_group_requested_as_another_release(
    env: Env, web: TestClient
) -> None:
    _, tok, fid = _catalog_world(env, web)
    with env.sessions.begin() as s:
        s.add(Request(user_id=env.user_id, release_mbid=rid(5000), release_group_mbid=rid(1)))
        h = FollowHit(
            follow_id=fid, release_group_mbid=rid(1), release_mbid=rid(7), status="skipped_cap"
        )
        s.add(h)
        s.flush()
        hid = h.id
    r = web.post(f"/watchlist/{fid}/hits/{hid}/request", data={"csrf_token": tok})
    assert "done=refused" in r.headers["location"]
    with env.sessions() as s:
        assert s.scalar(select(func.count()).select_from(Request)) == 1


def test_a_pick_that_finds_no_release_restores_or_removes_its_row(
    env: Env, web: TestClient
) -> None:
    watch, tok, fid = _catalog_world(env, web)
    with env.sessions.begin() as s:  # an earlier row for group 2, which the pick will take over
        s.add(
            FollowHit(
                follow_id=fid,
                release_group_mbid=rid(2),
                release_mbid=rid(8),
                title="New LP",
                status="skipped_cap",
            )
        )
    watch.no_releases = True
    web.post(f"/watchlist/{fid}/catalog", data={"csrf_token": tok, "groups": [rid(1), rid(2)]})
    _run_pick(env, watch)
    with env.sessions() as s:
        rows = {h.release_group_mbid: h for h in s.scalars(select(FollowHit))}
        assert rows[rid(2)].status == "skipped_cap" and rows[rid(2)].release_mbid == rid(8)
        assert rid(1) not in rows  # the pick created that row, so it goes


def test_follow_page_message_is_a_fixed_text(env: Env, web: TestClient) -> None:
    _, _, fid = _catalog_world(env, web)
    assert "Queued 3 for request" in web.get(f"/watchlist/{fid}?done=queued&n=3").text
    page = web.get(f"/watchlist/{fid}?msg=<b>hi</b>&done=nope").text
    assert "<b>hi</b>" not in page and "&lt;b&gt;" not in page


async def test_refollow_does_not_move_the_queued_check(env: Env) -> None:
    follow(env)
    far = env.clock.now() + timedelta(days=10)
    with env.sessions.begin() as s:
        for j in s.scalars(select(Job)):
            j.run_after = far
    follow(env)  # the same user again
    (job,) = env.jobs(JobKind.WATCHLIST_POLL)
    assert job.run_after.replace(tzinfo=UTC) == far


async def test_a_running_poll_elsewhere_does_not_stop_the_successor(env: Env) -> None:
    fid = follow(env)
    eid = entity_id(env, fid)
    with env.sessions.begin() as s:
        s.add(
            Job(
                kind=JobKind.WATCHLIST_POLL,
                payload={"entity_id": eid},
                status=JobState.RUNNING,
                run_after=env.clock.now(),
            )
        )
    await poll(env, FakeWatch(), eid)
    assert len([j for j in env.jobs(JobKind.WATCHLIST_POLL) if j.status == "queued"]) == 1


async def test_check_now_runs_before_a_backlog_of_searches(env: Env) -> None:
    fid = follow(env)
    eid = entity_id(env, fid)
    with env.sessions.begin() as s:
        for j in s.scalars(select(Job)):  # the follow's own check is days away
            j.run_after = env.clock.now() + timedelta(days=3)
        for _ in range(3):  # a catalogue pick queued these first
            enqueue_in(s, env.clock.now(), JobKind.SEARCH, {})
    with user_sessions(env.sessions).begin() as s:  # the button, from a web request
        e = s.get(WatchedEntity, eid)
        assert e and follows.check_now(s, env.clock.now(), e)
        enqueue_in(s, env.clock.now(), JobKind.SEARCH, {"request_id": 1})  # a new request
    first, second, third = (env.queue.claim("w") for _ in range(3))
    assert first and first.kind == JobKind.WATCHLIST_POLL
    assert second and second.payload == {"request_id": 1}
    assert third and third.kind == JobKind.SEARCH and third.payload == {}
