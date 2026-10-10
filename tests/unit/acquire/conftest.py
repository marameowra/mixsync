import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.pipeline import Pipeline
from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.core.jobs import JobKind, JobState
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.auth import Role, UserRole
from mixsync.db.models.work import Job
from mixsync.db.queue import JobQueue, JobRecord
from mixsync.library.fileops import FileOps
from mixsync.worker import Handler, run_job

from .fakes import (
    FakeAcoustId,
    FakeCoverArt,
    FakeMetadata,
    FakeSource,
    FakeTarget,
    FixedClock,
    make_fileops,
    make_pipeline,
)

LEASE = timedelta(seconds=60)


class Env:
    """Everything a test needs to drive the pipeline by hand."""

    def __init__(self, tmp_path: Path, settings: Settings, sessions: sessionmaker[Session]) -> None:
        self.settings, self.sessions = settings, sessions
        self.clock = FixedClock(datetime.now(UTC).replace(microsecond=0))
        self.queue = JobQueue(make_engine(settings), self.clock)
        self.data_dir, self.downloads = tmp_path / "data", tmp_path / "downloads"
        (self.data_dir / "library").mkdir(parents=True)
        self.downloads.mkdir()
        self.source = FakeSource(self.downloads, tmp_path / "scratch")
        self.acoustid, self.metadata, self.target = FakeAcoustId(), FakeMetadata(), FakeTarget()
        self.coverart = FakeCoverArt()
        self.ops: FileOps = make_fileops(sessions, self.clock, self.data_dir)
        self.pipeline: Pipeline = make_pipeline(
            sessions,
            self.clock,
            self.ops,
            settings.path_template,
            self.source,
            self.acoustid,
            self.metadata,
            self.coverart,
            self.target,
        )
        self.handlers: dict[JobKind, Handler] = self.pipeline.handlers()
        with sessions.begin() as s:
            self.user_id = create_user(s, "alice", "pw").id

    def next_due(self) -> datetime | None:
        with self.sessions() as s:
            t = s.scalar(
                select(Job.run_after)
                .where(Job.status.in_([JobState.QUEUED, JobState.RETRY_WAIT]))
                .order_by(Job.run_after)
            )
        return t and t.replace(tzinfo=UTC)

    async def step(self) -> JobRecord | None:
        """Run the next job; if only delayed ones remain, move the clock to the first. None
        when there is nothing left at all."""
        self.queue.reap()
        job = self.queue.claim("w", LEASE)
        if job is None:
            due = self.next_due()
            if due is None:
                return None
            self.clock.t = max(self.clock.t, due)
            self.queue.reap()
            job = self.queue.claim("w", LEASE)
            assert job
        await run_job(self.queue, job, "w", self.handlers, LEASE)
        return job

    async def drain(self, until: Callable[[], bool] = lambda: False, cap: int = 200) -> None:
        for _ in range(cap):
            if until() or await self.step() is None:
                return
        raise AssertionError(f"still busy after {cap} jobs")

    def jobs(self, kind: JobKind) -> list[Job]:
        with self.sessions() as s:
            return list(s.scalars(select(Job).where(Job.kind == kind).order_by(Job.id)))


@pytest.fixture
def sessions(settings: Settings) -> sessionmaker[Session]:
    upgrade()
    return make_session_factory(make_engine(settings))


@pytest.fixture
def env(tmp_path: Path, settings: Settings, sessions: sessionmaker[Session]) -> Env:
    return Env(tmp_path, settings, sessions)


@pytest.fixture
def web(env: Env, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("MIXSYNC_DATA_DIR", str(env.data_dir))
    monkeypatch.setenv("MIXSYNC_DOWNLOADS_DIR", str(env.downloads))
    with env.sessions.begin() as s:
        role = Role(name="none")
        s.add(role)
        s.flush()
        uid = create_user(s, "nocap", "pw").id
        s.execute(delete(UserRole).where(UserRole.user_id == uid))
        s.add(UserRole(user_id=uid, role_id=role.id))
    with TestClient(create_app(env.sessions, Settings()), follow_redirects=False) as c:
        yield c


def login(c: TestClient, user: str) -> str:
    """Log in; returns the CSRF token."""
    c.cookies.clear()
    assert c.post("/login", data={"username": user, "password": "pw"}).status_code == 303
    m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', c.get("/").text)
    assert m
    return m.group(1)
