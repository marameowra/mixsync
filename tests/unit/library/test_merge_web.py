"""The merge UI end to end: the routes against a real Merger, jobs run by the worker's run_job.
Lives here, not in tests/unit/web, for the `world` fixture."""

import asyncio
import re
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from mixsync.app import create_app
from mixsync.core.clock import SystemClock
from mixsync.core.config import Settings
from mixsync.core.jobs import JobKind
from mixsync.db.auth import create_user
from mixsync.db.models.work import Job
from mixsync.db.queue import JobQueue
from mixsync.library import fragmentation
from mixsync.worker import run_job

from .test_merge import CANON, GROUP, World, _rows, world  # noqa: F401  # world is a fixture


def _login(c: TestClient) -> str:
    c.post("/login", data={"username": "root", "password": "pw"})
    m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', c.get("/").text)
    assert m
    return m.group(1)


def _drain(w: World) -> None:
    lease = timedelta(minutes=5)
    q = JobQueue(w.sessions.kw["bind"], SystemClock())
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
    app.state.merger = w.merger
    return TestClient(app, follow_redirects=False)


def _jobs(w: World) -> list[Job]:
    with w.sessions() as s:
        return list(s.scalars(select(Job).where(Job.kind.in_([JobKind.MERGE, JobKind.UNDO_MERGE]))))


def _preview(c: TestClient, tok: str) -> int:
    r = c.post("/library/fragments/plan", data={"csrf_token": tok, "group": GROUP})
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


def test_build_confirm_run_undo(world: World) -> None:  # noqa: F811
    lib0, rows0 = world.lib(), [fragmentation.row_dict(t) for t in _rows(world)]
    with _client(world) as c:
        tok = _login(c)
        pid = _preview(c, tok)
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


def test_confirmed_draft_survives_new_preview(world: World) -> None:  # noqa: F811
    with _client(world) as c:
        tok = _login(c)
        pid = _preview(c, tok)
        assert c.post(f"/library/plans/{pid}/confirm", data={"csrf_token": tok}).status_code == 303
        r = c.post("/library/fragments/plan", data={"csrf_token": tok, "group": GROUP})
        assert r.status_code == 409
        _drain(world)
        assert world.plan(pid).status == "done", world.plan(pid).error
