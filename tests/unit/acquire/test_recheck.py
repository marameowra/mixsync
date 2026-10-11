"""Re-checking unverified albums: the job, its schedule, the promote plan and the UI."""

import hashlib
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from mixsync.acquire.merge import Merger
from mixsync.acquire.recheck import Rechecker
from mixsync.acquire.requests import RECHECK_INTERVALS, enqueue_recheck
from mixsync.core.jobs import JobKind, JobState
from mixsync.core.matching import AlbumInfo, TrackInfo
from mixsync.core.types import AcoustIdResult, Fingerprint, ReleaseRef
from mixsync.db.auth import create_user
from mixsync.db.models.auth import MatchingProfile, User
from mixsync.db.models.library import Plan, Track
from mixsync.library import fragmentation, tags
from mixsync.library.importer import Importer

from .conftest import Env, login
from .fakes import ALBUM, ALBUM_B, AUDIO, OTHER, RECORDINGS, FakeMetadata

KEY = ("Alpha Band", "Alpha Album", "2020")
WEEK = timedelta(weeks=1)


class Meta(FakeMetadata):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[ReleaseRef] = []
        self.searches: list[tuple[str, str, int]] = []

    async def search_releases(self, artist: str, album: str, limit: int = 10) -> list[ReleaseRef]:
        self.searches.append((artist, album, limit))
        return self.refs


class Lookup:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def lookup(self, fp: Fingerprint) -> list[AcoustIdResult]:
        self.calls.append(fp.fingerprint)
        return [AcoustIdResult("aid-" + fp.fingerprint, 0.97, (RECORDINGS[fp.fingerprint],))]


async def _fingerprint(path: Path) -> Fingerprint:
    return Fingerprint(1.0, tags.read_fields(path)["title"])


def _refs(*albums: AlbumInfo) -> list[ReleaseRef]:
    return [ReleaseRef(a.release_id or "", a.album, a.artist) for a in albums]


class Rig:
    def __init__(self, env: Env) -> None:
        self.env, self.meta, self.lookup = env, Meta(), Lookup()
        tpl = env.settings.path_template
        self.merger = Merger(env.sessions, env.clock, self.meta, env.ops, tpl)
        rechecker = Rechecker(
            env.sessions, env.clock, self.meta, _fingerprint, self.lookup, env.ops, tpl
        )
        env.handlers = env.pipeline.handlers() | rechecker.handlers() | self.merger.handlers()

    def seed(self) -> list[str]:
        """Two unverified files under the usual template, as an unverified import leaves them."""
        env = self.env
        files: list[tuple[Path, TrackInfo, str | None]] = []
        for i, t in enumerate(ALBUM.tracks, 1):
            src = env.downloads / f"{i}.mp3"
            shutil.copy(AUDIO / "cc0-tone-01.mp3", src)
            files.append((src, TrackInfo(t.title, medium_index=i, medium=1), None))
        album = AlbumInfo("Alpha Band", "Alpha Album", (), year=2020)
        results = Importer(env.ops, env.sessions, env.clock, env.settings.path_template)
        return [r.rel or "" for r in results.import_release(files, album, "unverified", "seed")]

    def schedule(self, user_id: int | None = None) -> None:
        with self.env.sessions.begin() as s:
            enqueue_recheck(s, self.env.clock.now(), KEY, user_id)

    def queued(self) -> list[tuple[int, datetime]]:
        """(attempt, due) of the waiting background checks."""
        return [
            (j.payload["attempt"], j.run_after.replace(tzinfo=UTC))
            for j in self.env.jobs(JobKind.RECHECK)
            if j.status is JobState.QUEUED and j.payload["user_id"] is None
        ]

    def plans(self, *states: str) -> list[Plan]:
        with self.env.sessions() as s:
            return list(s.scalars(select(Plan).where(Plan.status.in_(states or ["draft"]))))

    def rows(self) -> list[dict[str, object]]:
        with self.env.sessions() as s:
            return [fragmentation.row_dict(t) for t in s.scalars(select(Track).order_by(Track.id))]

    def library(self) -> dict[str, str]:
        lib = self.env.ops.library
        return {
            p.relative_to(lib).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(lib.rglob("*"))
            if p.is_file()
        }

    def trash(self) -> list[Path]:
        return [p for p in self.env.ops.trash_dir.rglob("*") if p.is_file()]


def _rig(env: Env, match: bool = True) -> Rig:
    rig = Rig(env)
    if match:  # four hits: only the top three are fetched
        rig.meta.refs = _refs(ALBUM, ALBUM_B, OTHER, ALBUM)
    return rig


def _in_window(due: datetime, now: datetime, interval: timedelta) -> bool:
    return interval / 2 <= due - now <= interval


async def test_no_match_schedules_the_next_check_within_its_backoff_window(env: Env) -> None:
    rig = _rig(env, match=False)
    rig.seed()
    rig.schedule()
    ((attempt, due),) = rig.queued()
    assert attempt == 0 and _in_window(due, env.clock.now(), RECHECK_INTERVALS[0])
    await env.step()
    assert rig.plans("draft", "discarded") == []
    assert rig.meta.searches == [("Alpha Band", "Alpha Album", 5)]
    assert rig.meta.calls == [] and rig.lookup.calls == []  # nothing to compare: no AcoustID
    ((attempt, due),) = rig.queued()
    assert attempt == 1 and _in_window(due, env.clock.now(), RECHECK_INTERVALS[1])


async def test_backoff_sequence_ends_quarterly(env: Env) -> None:
    rig = _rig(env, match=False)
    rig.seed()
    rig.schedule()
    assert [timedelta(weeks=1), timedelta(weeks=2), timedelta(days=30), timedelta(days=91)] == list(
        RECHECK_INTERVALS
    )
    for i in range(6):
        await env.step()
        ((attempt, due),) = rig.queued()
        assert attempt == i + 1  # 2 weeks, 30 days, 91 days, then 91 days again
        assert _in_window(due, env.clock.now(), RECHECK_INTERVALS[min(i + 1, 3)])


async def test_confident_match_makes_a_draft_promote_plan_and_stops_the_chain(env: Env) -> None:
    rig = _rig(env)
    old = rig.seed()
    rig.schedule()
    await env.step()
    assert rig.queued() == []  # found: the user decides now
    assert len(rig.meta.searches) == 1 and len(rig.meta.calls) == 3
    assert sorted(rig.lookup.calls) == ["Song One", "Song Two"]
    (plan,) = rig.plans()
    assert plan.kind == "promote" and plan.canonical_release_mbid == ALBUM.release_id
    assert plan.batch_id == f"promote-{plan.id}"
    new = [
        f"Alpha Band/Alpha Album (2020)/01-0{i} {t}.mp3"
        for i, t in ((1, "Song One"), (2, "Song Two"))
    ]
    assert [(o["op"], o["old"]["path"], o["new_path"]) for o in plan.ops] == [
        ("move", o, n) for o, n in zip(old, new, strict=True)
    ]
    tg = plan.ops[0]["tags"]
    assert tg["status"] == "verified" and tg["release_id"] == ALBUM.release_id
    assert tg["recording_id"] == RECORDINGS["Song One"] and tg["acoustid_id"] == "aid-Song One"
    assert {r["status"] for r in rig.rows()} == {"unverified"}  # a draft changes nothing


async def test_new_check_replaces_the_draft(env: Env) -> None:
    rig = _rig(env)
    rig.seed()
    rig.schedule()
    await env.step()
    (first,) = rig.plans()
    rig.schedule(user_id=env.user_id)
    await env.step()
    (second,) = rig.plans()
    assert second.id != first.id and [p.id for p in rig.plans("discarded")] == [first.id]


async def test_the_triggering_users_profile_decides(env: Env) -> None:
    rig = _rig(env)
    rig.seed()
    with env.sessions.begin() as s:
        p = MatchingProfile(name="never", preset="custom", auto_accept_max=-1.0, review_max=0.4)
        s.add(p)
        s.flush()
        s.get_one(User, env.user_id).matching_profile_id = p.id
    rig.schedule(user_id=env.user_id)
    await env.step()
    assert rig.plans("draft", "discarded") == []
    assert rig.queued() == []  # a user's check does not touch the background schedule


async def test_promote_runs_and_undoes_through_the_plan_pages(env: Env, web: TestClient) -> None:
    rig = _rig(env)
    old = rig.seed()
    lib0, rows0 = rig.library(), rig.rows()
    with env.sessions.begin() as s:
        create_user(s, "root", "pw", admin=True)
    tok = login(web, "root")
    form = {"csrf_token": tok, "albumartist": KEY[0], "album": KEY[1], "year": KEY[2]}
    assert web.post("/library/unverified/check", data={**form, "album": "x"}).status_code == 404
    assert web.post("/library/unverified/check", data=form).status_code == 303
    assert web.post("/library/unverified/check", data=form).status_code == 303
    (job,) = env.jobs(JobKind.RECHECK)  # idempotent while one waits
    assert job.payload["user_id"] is not None
    await env.step()
    (plan,) = rig.plans()
    html = web.get("/library/unverified").text
    assert "Check MusicBrainz now" in html and f"/library/plans/{plan.id}" in html
    assert "Last checked: " + env.clock.now().strftime("%Y-%m-%d") in html
    assert "1 album ready to promote" in web.get("/").text
    page = web.get(f"/library/plans/{plan.id}").text
    assert "Promote plan" in page and "unverified &rarr; verified" in page and old[0] in page
    assert "Confirm promotion" in page

    assert (
        web.post(f"/library/plans/{plan.id}/confirm", data={"csrf_token": tok}).status_code == 303
    )
    await env.drain(until=lambda: rig.plans("done") != [])
    assert [r["status"] for r in rig.rows()] == ["verified", "verified"]
    assert [r["recording_mbid"] for r in rig.rows()] == [
        RECORDINGS["Song One"],
        RECORDINGS["Song Two"],
    ]
    assert {r["release_mbid"] for r in rig.rows()} == {ALBUM.release_id}
    assert sorted(rig.library()) == sorted(r["path"] for r in rig.rows())  # type: ignore[type-var]
    for r in rig.rows():
        t = tags.read_fields(env.ops.library / str(r["path"]))
        assert t["mixsync_status"] == "verified" and t["musicbrainz_trackid"] == r["recording_mbid"]
    assert len(rig.trash()) == 2  # the originals
    assert "Nothing unverified" in web.get("/library/unverified").text
    assert "ready to promote" not in web.get("/").text

    assert web.post(f"/library/plans/{plan.id}/undo", data={"csrf_token": tok}).status_code == 303
    await env.drain(until=lambda: rig.plans("undone") != [])
    assert rig.library() == lib0 and rig.rows() == rows0
    t = tags.read_fields(env.ops.library / old[0])
    assert t["mixsync_status"] == "unverified" and "musicbrainz_trackid" not in t


async def test_checks_stop_when_nothing_is_unverified(env: Env) -> None:
    rig = _rig(env)
    rig.schedule()  # no such album at all
    await env.step()
    assert rig.queued() == [] and rig.meta.searches == []
    rig.seed()
    rig.schedule()
    with env.sessions.begin() as s:
        for t in s.scalars(select(Track)):
            t.status = "verified"  # promoted meanwhile
    await env.step()
    assert rig.queued() == [] and rig.meta.searches == [] and rig.plans("draft") == []


async def test_a_confirmed_plan_is_not_replaced(env: Env) -> None:
    rig = _rig(env)
    rig.seed()
    rig.schedule()
    await env.step()
    (plan,) = rig.plans()
    with env.sessions.begin() as s:
        s.get_one(Plan, plan.id).status = "running"
    rig.schedule(user_id=env.user_id)
    await env.step()
    assert [p.id for p in rig.plans("running")] == [plan.id] and rig.plans() == []


def test_one_background_check_waits_per_album(env: Env) -> None:
    rig = _rig(env)
    rig.schedule()
    rig.schedule()
    assert len(rig.queued()) == 1
