"""Re-checking unverified albums: the job, its schedule, the promote plan and the UI."""

import asyncio
import hashlib
import shutil
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mixsync.acquire.merge import Merger, PlanInProgress
from mixsync.acquire.recheck import Rechecker, statuses
from mixsync.acquire.requests import (
    RECHECK_INTERVALS,
    enqueue_recheck,
    group,
    read_unverified,
)
from mixsync.core.errors import SafetyError, TransientError
from mixsync.core.jobs import JobKind, JobState
from mixsync.core.matching import AlbumInfo, TrackInfo
from mixsync.core.types import AcoustIdResult, Fingerprint, ReleaseRef
from mixsync.db.auth import create_user
from mixsync.db.models.auth import MatchingProfile, User
from mixsync.db.models.library import Plan, Track
from mixsync.db.models.work import Job
from mixsync.library import fragmentation, tags
from mixsync.library.importer import Importer, tagset_for
from mixsync.worker import run_worker

from .conftest import Env, login
from .fakes import ALBUM, ALBUM_B, AUDIO, OTHER, RECORDINGS, FakeMetadata

KEY = ("Alpha Band", "Alpha Album", "2020")
WEEK = timedelta(weeks=1)


class Meta(FakeMetadata):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[ReleaseRef] = []
        self.searches: list[tuple[str, str, int]] = []
        self.fail: Exception | None = None

    async def search_releases(self, artist: str, album: str, limit: int = 10) -> list[ReleaseRef]:
        self.searches.append((artist, album, limit))
        if self.fail:
            raise self.fail
        return self.refs


class Lookup:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def lookup(self, fp: Fingerprint) -> list[AcoustIdResult]:
        self.calls.append(fp.fingerprint)
        return [AcoustIdResult("aid-" + fp.fingerprint, 0.97, (RECORDINGS[fp.fingerprint],))]


BAD: set[str] = set()  # titles whose files cannot be fingerprinted


async def _fingerprint(path: Path) -> Fingerprint:
    title = tags.read_fields(path)["title"]
    if title in BAD:
        raise RuntimeError("fpcalc failed")
    return Fingerprint(1.0, title)


def _refs(*albums: AlbumInfo) -> list[ReleaseRef]:
    return [ReleaseRef(a.release_id or "", a.album, a.artist) for a in albums]


class Rig:
    def __init__(self, env: Env) -> None:
        self.env, self.meta, self.lookup = env, Meta(), Lookup()
        tpl = env.settings.path_template
        self.merger = Merger(env.sessions, env.clock, self.meta, env.ops, tpl)
        BAD.clear()
        self.rechecker = rechecker = Rechecker(
            env.sessions, env.clock, self.meta, _fingerprint, self.lookup, env.ops, tpl
        )
        env.handlers = env.pipeline.handlers() | rechecker.handlers() | self.merger.handlers()

    def seed(self, year: int | None = 2020) -> list[str]:
        """Two unverified files under the usual template, as an unverified import leaves them."""
        env = self.env
        files: list[tuple[Path, TrackInfo, str | None]] = []
        for i, t in enumerate(ALBUM.tracks, 1):
            src = env.downloads / f"{year}-{i}.mp3"
            shutil.copy(AUDIO / "cc0-tone-01.mp3", src)
            files.append((src, TrackInfo(t.title, medium_index=i, medium=1), None))
        album = AlbumInfo("Alpha Band", "Alpha Album", (), year=year)
        results = Importer(env.ops, env.sessions, env.clock, env.settings.path_template)
        return [
            r.rel or "" for r in results.import_release(files, album, "unverified", f"seed-{year}")
        ]

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


async def test_a_failed_plan_blocks_a_new_draft_and_is_linked(env: Env) -> None:
    rig = _rig(env)
    rig.seed()
    rig.schedule()
    await env.step()
    (plan,) = rig.plans()
    with env.sessions.begin() as s:
        s.get_one(Plan, plan.id).status = "failed"
        st = statuses(s, group(read_unverified(s, env.ops.library)))
    assert [x.plan_id for x in st.values()] == [plan.id]
    rig.schedule(user_id=env.user_id)
    await env.step()
    assert [p.id for p in rig.plans("failed")] == [plan.id] and rig.plans() == []


def test_one_background_check_waits_per_album(env: Env) -> None:
    rig = _rig(env)
    rig.schedule()
    rig.schedule()
    assert len(rig.queued()) == 1


TWO = {"Song One", "Song Two"}


async def test_a_file_that_cannot_be_fingerprinted_is_skipped(env: Env) -> None:
    rig = _rig(env)
    rig.seed()
    BAD.add("Song Two")
    rig.schedule()
    await env.step()
    assert rig.lookup.calls == ["Song One"]  # the other file was still looked up and scored
    assert env.jobs(JobKind.RECHECK)[0].status is JobState.SUCCEEDED  # no failure


async def test_no_file_to_fingerprint_is_no_match_and_the_chain_goes_on(env: Env) -> None:
    rig = _rig(env)
    rig.seed()
    BAD.update(TWO)
    rig.schedule()
    await env.step()
    assert rig.plans("draft") == [] and [a for a, _ in rig.queued()] == [1]


async def test_a_failing_run_still_queues_the_next_check_once(env: Env) -> None:
    rig = _rig(env)
    rig.seed()
    rig.meta.fail = TransientError("musicbrainz is down")
    rig.schedule()
    await env.step()  # fails, retry_wait
    assert [a for a, _ in rig.queued()] == [1]
    await env.step()  # the retry fails the same way: still one continuation
    nexts = [j for j in env.jobs(JobKind.RECHECK) if j.payload["attempt"] == 1]
    assert len(nexts) == 1


async def test_a_retried_job_adds_one_continuation(env: Env) -> None:
    rig = _rig(env, match=False)
    rig.seed()
    rig.schedule()
    env.clock.advance(timedelta(weeks=2))
    env.queue.reap()
    job = env.queue.claim("w", timedelta(minutes=5))
    assert job
    await rig.rechecker.recheck(job)
    await rig.rechecker.recheck(job)
    assert [a for a, _ in rig.queued()] == [1]


def _verified(env: Env, path: str, recording: str, **kw: object) -> None:
    with env.sessions.begin() as s:
        s.add(
            Track(
                path=path,
                recording_mbid=recording,
                status="verified",
                batch_id="b",
                imported_at=env.clock.now(),
                **kw,
            )
        )


async def test_partial_match_keeps_files_and_the_chain_goes_on(env: Env) -> None:
    rig = _rig(env)
    old = rig.seed()
    _verified(env, "v/one.mp3", RECORDINGS["Song One"])  # already in the library
    rig.schedule()
    await env.step()
    (plan,) = rig.plans()
    assert [o["op"] for o in plan.ops] == ["keep", "move"]
    assert plan.ops[0]["reason"] == "already owned as verified"
    assert plan.ops[0]["old"]["path"] == old[0]
    assert [a for a, _ in rig.queued()] == [1]  # one file is still unverified
    # the next background check replaces the draft and queues the one after
    await env.step()
    (second,) = rig.plans()
    assert second.id != plan.id and [a for a, _ in rig.queued()] == [2]


async def test_nothing_to_promote_makes_no_plan(env: Env) -> None:
    rig = _rig(env)
    rig.seed()
    _verified(env, "v/one.mp3", RECORDINGS["Song One"])
    _verified(env, "v/two.mp3", RECORDINGS["Song Two"])
    rig.schedule()
    await env.step()
    assert rig.plans("draft") == [] and [a for a, _ in rig.queued()] == [1]


async def test_merge_leaves_a_promote_draft_and_waits_for_a_confirmed_one(env: Env) -> None:
    rig = _rig(env)
    rig.seed()
    rig.schedule()
    await env.step()
    (promote,) = rig.plans()
    # a verified track of the same group, on another release
    _verified(
        env,
        "v/b.mp3",
        RECORDINGS["Song One"],
        release_mbid=ALBUM_B.release_id,
        release_group_mbid=ALBUM.release_group_id,
    )
    merge_id = await rig.merger.build(str(ALBUM.release_group_id))
    assert [p.id for p in rig.plans()] == [promote.id, merge_id]  # both drafts stand
    with env.sessions.begin() as s:
        s.get_one(Plan, promote.id).status = "running"
    with pytest.raises(PlanInProgress):
        await rig.merger.build(str(ALBUM.release_group_id))


async def test_merge_keeps_the_other_columns_of_a_row(env: Env) -> None:
    rig = _rig(env)
    _verified(
        env,
        "v/b.mp3",
        RECORDINGS["Song One"],
        release_mbid=ALBUM_B.release_id,
        release_group_mbid="other-group",
    )
    path = env.data_dir / "library" / "v" / "b.mp3"
    path.parent.mkdir()
    shutil.copy(AUDIO / "cc0-tone-01.mp3", path)
    with env.sessions.begin() as s:
        t = s.scalars(select(Track)).one()
        ts = tagset_for(ALBUM.tracks[0], ALBUM, None, "verified")  # another release group
        plan = Plan(
            kind="merge",
            release_group_mbid="other-group",
            canonical_release_mbid=str(ALBUM.release_id),
            batch_id="merge-x",
            ops=[
                {
                    "op": "move",
                    "new_path": "v/c.mp3",
                    "track_id": t.id,
                    "old": fragmentation.row_dict(t),
                    "done": False,
                    "tags": asdict(ts),
                }
            ],
        )
        s.add(plan)
        s.flush()
        pid = plan.id
    rig.merger.execute(pid)
    with env.sessions() as s:
        row = s.scalars(select(Track)).one()
    assert row.path == "v/c.mp3" and row.release_mbid == ALBUM.release_id
    assert row.release_group_mbid == "other-group"  # not ALBUM's group


class Boom(BaseException):
    pass


async def test_promote_resumes_after_a_crash(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _rig(env)
    rig.seed()
    rig.schedule()
    await env.step()
    (plan,) = rig.plans()
    real = Merger._commit_op  # pyright: ignore[reportPrivateUsage]
    hits: list[int] = []

    def crash(self: Merger, *a: Any, **k: Any) -> None:
        hits.append(1)
        if len(hits) == 1:
            raise Boom
        real(self, *a, **k)

    monkeypatch.setattr(Merger, "_commit_op", crash)
    with pytest.raises(Boom):
        rig.merger.execute(plan.id)
    assert rig.plans("running") != []
    rig.merger.execute(plan.id)  # the retry resumes
    assert rig.plans("done") != [] and {r["status"] for r in rig.rows()} == {"verified"}
    assert len(rig.trash()) == 2


async def test_promote_refuses_a_row_changed_since_the_preview(env: Env) -> None:
    rig = _rig(env)
    rig.seed()
    rig.schedule()
    await env.step()
    (plan,) = rig.plans()
    lib = rig.library()
    with env.sessions.begin() as s:
        s.scalars(select(Track)).first().acoustid_id = "edited"  # type: ignore[union-attr]
    with pytest.raises(SafetyError):
        rig.merger.execute(plan.id)
    assert rig.plans("failed") != [] and rig.library() == lib


async def test_undo_of_a_promotion_restarts_the_background_chain(env: Env) -> None:
    rig = _rig(env)
    rig.seed()
    rig.schedule()
    await env.step()
    (plan,) = rig.plans()
    rig.merger.execute(plan.id)
    assert rig.queued() == []
    rig.merger.undo_plan(plan.id)
    assert [a for a, _ in rig.queued()] == [0]


def test_start_chains_covers_every_unverified_album_once(env: Env) -> None:
    rig = _rig(env)
    rig.seed()
    rig.rechecker.start_chains()
    rig.rechecker.start_chains()
    ((attempt, due),) = rig.queued()
    assert attempt == 0 and _in_window(due, env.clock.now(), RECHECK_INTERVALS[0])


async def test_discard_a_draft_plan(env: Env, web: TestClient) -> None:
    rig = _rig(env)
    rig.seed()
    rig.schedule()
    await env.step()
    (plan,) = rig.plans()
    with env.sessions.begin() as s:
        create_user(s, "root", "pw", admin=True)
    tok = login(web, "root")
    url = f"/library/plans/{plan.id}/discard"
    assert "Discard plan" in web.get(f"/library/plans/{plan.id}").text
    assert web.post(url).status_code == 403  # no CSRF token
    confirm = f"/library/plans/{plan.id}/confirm"
    assert web.post(confirm, data={"csrf_token": tok}).status_code == 303
    assert web.post(url, data={"csrf_token": tok}).status_code == 409  # its job is waiting
    with env.sessions.begin() as s:
        for j in s.scalars(select(Job).where(Job.kind == JobKind.MERGE)):
            j.status = JobState.CANCELLED
    assert web.post(url, data={"csrf_token": tok}).status_code == 303
    assert rig.plans("discarded") != [] and rig.plans() == []
    assert web.post(url, data={"csrf_token": tok}).status_code == 409
    assert "ready to promote" not in web.get("/").text


async def test_undo_queues_the_check_under_the_grouped_key(env: Env) -> None:
    rig = _rig(env)
    rig.seed(None)  # undated
    with env.sessions.begin() as s:  # the undated key, as the import scheduled it
        enqueue_recheck(s, env.clock.now(), ("Alpha Band", "Alpha Album", ""), None)
    await env.step()
    (plan,) = rig.plans()
    rig.merger.execute(plan.id)
    rig.seed(2020)  # a single dated album of the same name turns up
    rig.merger.undo_plan(plan.id)
    (job,) = [j for j in env.jobs(JobKind.RECHECK) if j.status is JobState.QUEUED]
    assert job.payload["album"] == list(KEY)  # the undated files fold into the dated album


async def test_any_error_in_a_background_run_still_queues_the_next_check(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(env)
    rig.seed()

    def broken(*_a: object) -> bool:
        raise ValueError("boom")

    monkeypatch.setattr(Rechecker, "_draft", broken)
    rig.schedule()
    await env.step()
    assert [j.status for j in env.jobs(JobKind.RECHECK)][0] is JobState.FAILED
    assert [a for a, _ in rig.queued()] == [1]


async def test_startup_runs_in_the_worker_task_before_the_first_claim() -> None:
    order: list[str] = []

    class Queue:
        def reap(self) -> None:
            order.append("reap")

        def claim(self, *_a: object) -> None:
            stop.set()

    stop = asyncio.Event()
    await run_worker(Queue(), "w", stop, {}, startup=lambda: order.append("startup"))  # type: ignore[arg-type]
    assert order == ["startup", "reap"]


async def test_a_failed_startup_scan_does_not_stop_the_worker() -> None:
    claimed: list[bool] = []

    class Queue:
        def reap(self) -> None:
            pass

        def claim(self, *_a: object) -> None:
            claimed.append(True)
            stop.set()

    def boom() -> None:
        raise RuntimeError("database is locked")

    stop = asyncio.Event()
    await run_worker(Queue(), "w", stop, {}, startup=boom)  # type: ignore[arg-type]
    assert claimed == [True]


async def test_a_locked_database_while_polling_does_not_stop_the_worker() -> None:
    calls: list[str] = []

    class Queue:
        def reap(self) -> None:
            calls.append("reap")
            if len(calls) == 1:
                raise RuntimeError("database is locked")

        def claim(self, *_a: object) -> None:
            calls.append("claim")
            stop.set()

    stop = asyncio.Event()
    await run_worker(Queue(), "w", stop, {}, poll=0)  # type: ignore[arg-type]
    assert calls == ["reap", "reap", "claim"]
