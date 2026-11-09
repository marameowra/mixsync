"""The merge UI end to end: the routes against a real Merger, jobs run by the worker's run_job.
Lives here, not in tests/unit/web, for the `world` fixture."""

import asyncio
import re
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mixsync.app import create_app
from mixsync.core.clock import SystemClock
from mixsync.core.config import Settings
from mixsync.core.errors import PermanentError, TransientError
from mixsync.core.jobs import JobKind, JobState
from mixsync.db.auth import create_user
from mixsync.db.models.work import Job
from mixsync.db.queue import JobQueue
from mixsync.library import fragmentation
from mixsync.worker import run_job

from .conftest import MakeSrc
from .test_merge import CANON, GROUP, OLD, World, _rows, world  # noqa: F401  # world is a fixture


def _login(c: TestClient) -> str:
    c.post("/login", data={"username": "root", "password": "pw"})
    m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', c.get("/").text)
    assert m
    return m.group(1)


def _drain(w: World) -> None:
    lease = timedelta(minutes=5)
    q = JobQueue(w.sessions.kw["bind"], SystemClock(), lambda *_: None)
    handlers = w.merger.handlers()
    while (job := q.claim("t", lease)) is not None:
        if job.kind in handlers:
            asyncio.run(run_job(q, job, "t", handlers, lease))
        else:  # e.g. the rescan a finished merge queues
            q.start(job.id, "t")
            q.complete(job.id, "t")


def _client(w: World) -> TestClient:
    with w.sessions.begin() as s:
        create_user(s, "root", "pw", admin=True)
    app = create_app(w.sessions, Settings())
    app.state.services = replace(app.state.services, merger=w.merger)
    return TestClient(app, follow_redirects=False)


def _jobs(w: World) -> list[Job]:
    with w.sessions() as s:
        return list(s.scalars(select(Job).where(Job.kind.in_([JobKind.MERGE, JobKind.UNDO_MERGE]))))


BUILD = f"/library/fragments/plan?group={GROUP}"


def _builds(w: World) -> list[Job]:
    with w.sessions() as s:
        return list(s.scalars(select(Job).where(Job.kind == JobKind.MERGE_PLAN).order_by(Job.id)))


def _post(c: TestClient, tok: str) -> None:
    r = c.post("/library/fragments/plan", data={"csrf_token": tok, "group": GROUP})
    assert (r.status_code, r.headers["location"]) == (303, BUILD), r.text


def _preview(c: TestClient, tok: str, w: World) -> int:
    """Queue the build, let the worker run it once, then follow the progress page to the plan."""
    _post(c, tok)
    _drain(w)
    r = c.get(BUILD)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


def test_build_confirm_run_undo(world: World) -> None:  # noqa: F811
    lib0, rows0 = world.lib(), [fragmentation.row_dict(t) for t in _rows(world)]
    with _client(world) as c:
        tok = _login(c)
        pid = _preview(c, tok, world)
        confirm = f"/library/plans/{pid}/confirm"
        assert c.post(confirm, data={"csrf_token": tok}).status_code == 303
        assert c.post(confirm, data={"csrf_token": tok}).status_code == 303
        assert [j.kind for j in _jobs(world)] == [JobKind.MERGE]
        assert c.post(f"/library/plans/{pid}/undo", data={"csrf_token": tok}).status_code == 409
        _drain(world)
        assert world.plan(pid).status == "done", world.plan(pid).error
        assert world.canonical() == CANON
        assert sorted(p for p, _ in world.tracks().values()) == sorted(world.lib())
        undo = f"/library/plans/{pid}/undo"
        assert c.post(undo, data={"csrf_token": tok}).status_code == 303
        assert c.post(undo, data={"csrf_token": tok}).status_code == 409
        _drain(world)
        assert world.plan(pid).status == "undone", world.plan(pid).error
        assert world.lib() == lib0
        assert [fragmentation.row_dict(t) for t in _rows(world)] == rows0
        assert world.canonical() is None


def test_confirm_after_the_merge_ran_is_a_no_op(world: World) -> None:  # noqa: F811
    """The plan page does not poll: a second click on Confirm once the job ran is not a 409."""
    with _client(world) as c:
        tok = _login(c)
        pid = _preview(c, tok, world)
        confirm = f"/library/plans/{pid}/confirm"
        assert c.post(confirm, data={"csrf_token": tok}).status_code == 303
        _drain(world)
        assert world.plan(pid).status == "done", world.plan(pid).error
        assert c.post(confirm, data={"csrf_token": tok}).status_code == 303
        assert [j.kind for j in _jobs(world)] == [JobKind.MERGE]
        assert c.post(f"/library/plans/{pid}/discard", data={"csrf_token": tok}).status_code == 409


def test_merged_group_leaves_the_list(world: World, make_src: MakeSrc) -> None:  # noqa: F811
    """The bonus track the merge kept on the old release does not list the group again; a new
    off-canonical import does."""
    with _client(world) as c:
        tok = _login(c)
        pid = _preview(c, tok, world)
        assert "keep" in [op["op"] for op in world.plan_ops(pid)]
        assert c.post(f"/library/plans/{pid}/confirm", data={"csrf_token": tok}).status_code == 303
        _drain(world)
        assert world.plan(pid).status == "done", world.plan(pid).error
        with world.sessions() as s:
            assert fragmentation.find(s) == []
        world.imp(make_src("flac", "again"), OLD, "r1", batch="later")
        with world.sessions() as s:
            assert [f.group_mbid for f in fragmentation.find(s)] == [GROUP]


def test_confirmed_draft_survives_new_preview(world: World) -> None:  # noqa: F811
    with _client(world) as c:
        tok = _login(c)
        pid = _preview(c, tok, world)
        assert c.post(f"/library/plans/{pid}/confirm", data={"csrf_token": tok}).status_code == 303
        r = c.post("/library/fragments/plan", data={"csrf_token": tok, "group": GROUP})
        assert r.status_code == 409
        _drain(world)
        assert world.plan(pid).status == "done", world.plan(pid).error


class NoMusicBrainz:
    """Fails the test if anything calls MusicBrainz."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"MusicBrainz called during the request: {name}")


def test_post_queues_one_build_without_musicbrainz(world: World) -> None:  # noqa: F811
    with _client(world) as c:
        tok = _login(c)
        c.app.state.services = replace(  # type: ignore[attr-defined]
            c.app.state.services,  # type: ignore[attr-defined]
            merger=replace(world.merger, metadata=NoMusicBrainz()),
        )
        _post(c, tok)
        _post(c, tok)  # a second click points at the queued build
        assert len(_builds(world)) == 1
        page = c.get(BUILD)
        assert page.status_code == 200 and "Building the merge plan" in page.text
        assert 'hx-trigger="every 5s"' in page.text


def test_finished_build_redirects_to_the_plan_within_one_poll(world: World) -> None:  # noqa: F811
    with _client(world) as c:
        tok = _login(c)
        _post(c, tok)
        _drain(world)  # one worker pass; the fake provider answers at once, like a cache hit
        r = c.get(BUILD, headers={"HX-Request": "true"})
        pid = world.plan(int(r.headers["HX-Redirect"].rsplit("/", 1)[1])).id
        assert c.get(BUILD).headers["location"] == f"/library/plans/{pid}"
        html = c.get(f"/library/plans/{pid}").text
        assert world.plan(pid).status == "draft" and "Confirm merge" in html


@pytest.mark.parametrize(
    ("error", "stored"),  # the real client raises TransientError; the job wraps a bare httpx error
    [
        (TransientError("musicbrainz 503"), "musicbrainz 503"),
        (httpx.ConnectTimeout("slow"), "MusicBrainz is unavailable"),
    ],
)
def test_transient_musicbrainz_error_retries(world: World, error: Exception, stored: str) -> None:  # noqa: F811
    async def down(mbid: str) -> object:
        raise error

    world.metadata.get_release = down  # type: ignore[method-assign]
    with _client(world) as c:
        tok = _login(c)
        _post(c, tok)
        _drain(world)
        (job,) = _builds(world)
        assert job.status == JobState.RETRY_WAIT and stored in job.last_error
        page = c.get(BUILD)
        assert page.status_code == 200 and "trying again shortly" in page.text


def test_permanent_error_shows_message_and_retry(world: World) -> None:  # noqa: F811
    real = world.metadata.get_release

    async def gone(mbid: str) -> object:
        raise PermanentError(f"release {mbid} returned 404")

    world.metadata.get_release = gone  # type: ignore[method-assign]
    with _client(world) as c:
        tok = _login(c)
        _post(c, tok)
        _drain(world)
        assert [j.status for j in _builds(world)] == [JobState.FAILED]
        page = c.get(BUILD).text
        assert f"release {CANON} returned 404" in page and "Try again" in page
        assert 'name="csrf_token"' in page and "hx-trigger" not in page
        world.metadata.get_release = real  # type: ignore[method-assign]
        pid = _preview(c, tok, world)  # the retry button posts the same form
        assert len(_builds(world)) == 2 and world.plan(pid).status == "draft"


def test_group_without_verified_tracks_fails_with_message_and_retry(world: World) -> None:  # noqa: F811
    other = "00000000-0000-4000-8000-000000000000"
    url = f"/library/fragments/plan?group={other}"
    with _client(world) as c:
        tok = _login(c)
        r = c.post("/library/fragments/plan", data={"csrf_token": tok, "group": other})
        assert (r.status_code, r.headers["location"]) == (303, url)
        _drain(world)
        with world.sessions() as s:
            (job,) = s.scalars(select(Job).where(Job.kind == JobKind.MERGE_PLAN))
            assert job.status == JobState.FAILED  # a LookupError is not retried
        page = c.get(url).text
        assert f"no verified tracks in release group {other}" in page and "Try again" in page
