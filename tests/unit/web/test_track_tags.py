import re
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.app import create_app
from mixsync.core.clock import SystemClock
from mixsync.core.config import Settings
from mixsync.core.jobs import JobKind, JobState
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.journal import SqlJournal
from mixsync.db.migrate import upgrade
from mixsync.db.models.library import Plan, Track
from mixsync.db.models.safety import TagSnapshot
from mixsync.db.models.work import Job
from mixsync.library import tags
from mixsync.library.fileops import FileOps
from mixsync.library.importer import snapshotting_writer

AUDIO = Path(__file__).parents[2] / "fixtures" / "audio" / "cc0-tone-01.flac"
REL = "A/B/01 T.flac"
NEW = tags.TagSet(title="Imported", artist="X", album="Al", albumartist="X", track=1, disc=1)


class Env:
    def __init__(self, tmp_path: Path, settings: Settings) -> None:
        upgrade()
        self.sf: sessionmaker[Session] = make_session_factory(make_engine(settings))
        self.data = tmp_path / "data"
        (self.data / "library").mkdir(parents=True)
        src = tmp_path / "src.flac"
        shutil.copy(AUDIO, src)
        tags.write(src, tags.TagSet("Original", "O", "OA", "OA", 7, 2))
        clock = SystemClock()
        ops = FileOps(self.data, tmp_path / "dl", SqlJournal(self.sf, clock), clock)
        ops.import_file(src, REL, snapshotting_writer(self.sf, clock, REL, NEW), "b1")
        with self.sf() as s, s.begin():
            row = Track(path=REL, status="verified", batch_id="b1", imported_at=datetime.now(UTC))
            s.add(row)
            create_user(s, "alice", "pw")
            create_user(s, "root", "pw", admin=True)
            s.flush()
            self.track = row.id
        self.file = self.data / "library" / REL
        self.url = f"/library/tracks/{self.track}/tags"

    def title(self) -> str:
        return tags.read_fields(self.file)["title"]

    def snapshots(self) -> int:
        with self.sf() as s:
            return len(list(s.scalars(select(TagSnapshot))))


@pytest.fixture
def env(tmp_path: Path, settings: Settings) -> Env:
    return Env(tmp_path, settings)


@pytest.fixture
def client(env: Env, settings: Settings) -> Iterator[TestClient]:
    settings.data_dir = str(env.data)
    with TestClient(create_app(env.sf, settings), follow_redirects=False) as c:
        yield c


def _login(c: TestClient, user: str = "root") -> str:
    c.cookies.clear()
    assert c.post("/login", data={"username": user, "password": "pw"}).status_code == 303
    m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', c.get("/").text)
    assert m
    return m.group(1)


def _snap(client: TestClient, env: Env) -> str:
    m = re.search(r'name="snapshot_id" value="(\d+)"', client.get(env.url).text)
    assert m
    return m.group(1)


def test_preview_shows_diff(client: TestClient, env: Env) -> None:
    _login(client)
    html = client.get(env.url).text
    assert "Original" in html and "Imported" in html and "Restore these tags" in html
    assert "not changed" in html
    assert env.title() == "Imported"  # a preview writes nothing


def test_restore_then_undo_the_restore(client: TestClient, env: Env) -> None:
    tok = _login(client)
    before = env.snapshots()
    r = client.post(env.url, data={"csrf_token": tok, "snapshot_id": _snap(client, env)})
    assert r.status_code == 303
    assert env.title() == "Original"
    assert env.snapshots() == before + 1
    # the newest snapshot now holds the pre-undo tags, so the same action reverses it
    r = client.post(env.url, data={"csrf_token": tok, "snapshot_id": _snap(client, env)})
    assert r.status_code == 303
    assert env.title() == "Imported"
    with env.sf() as s:
        assert s.scalars(select(Track)).one().path == REL
    assert len(list((env.data / ".trash").rglob("*.flac"))) == 2  # each replaced copy is kept


def test_missing_file_no_write(client: TestClient, env: Env) -> None:
    tok = _login(client)
    snap = _snap(client, env)
    env.file.unlink()
    assert client.get(env.url).status_code == 409
    r = client.post(env.url, data={"csrf_token": tok, "snapshot_id": snap})
    assert r.status_code == 409 and not env.file.exists()


def test_snapshot_of_another_path_refused(client: TestClient, env: Env) -> None:
    tok = _login(client)
    with env.sf() as s, s.begin():
        first = s.scalars(select(TagSnapshot)).one()
        other = TagSnapshot(
            file_path="other.flac", op_id=first.op_id, tags=first.tags, taken_at=first.taken_at
        )
        s.add(other)
        s.flush()
        bad = other.id
    r = client.post(env.url, data={"csrf_token": tok, "snapshot_id": bad})
    assert r.status_code == 409 and env.title() == "Imported"


def test_csrf_and_role(client: TestClient, env: Env) -> None:
    _login(client)
    snap = _snap(client, env)
    assert client.post(env.url, data={"snapshot_id": snap}).status_code == 403
    tok = _login(client, "alice")
    assert client.get(env.url).status_code == 403
    assert client.post(env.url, data={"csrf_token": tok, "snapshot_id": snap}).status_code == 403
    assert env.title() == "Imported" and env.snapshots() == 1


def test_snapshot_of_a_replaced_file_refused(client: TestClient, env: Env) -> None:
    # another recording ended up at this path (merge, trash, undo): the path's snapshot is not its
    tok = _login(client)
    snap = _snap(client, env)
    tags.write(env.file, tags.TagSet("Other recording", "Z", "Z", "Z", 1, 1))
    assert client.get(env.url).status_code == 409
    r = client.post(env.url, data={"csrf_token": tok, "snapshot_id": snap})
    assert r.status_code == 409 and env.title() == "Other recording"


def test_stale_second_post_is_refused(client: TestClient, env: Env) -> None:
    tok = _login(client)
    stale = _snap(client, env)  # both clicks carry the same preview
    assert client.post(env.url, data={"csrf_token": tok, "snapshot_id": stale}).status_code == 303
    snapshots = env.snapshots()
    assert client.post(env.url, data={"csrf_token": tok, "snapshot_id": stale}).status_code == 409
    assert env.title() == "Original" and env.snapshots() == snapshots


@pytest.mark.parametrize("status", ["running", "undoing", "draft"])
def test_refused_while_a_plan_is_changing_the_track(
    client: TestClient, env: Env, status: str
) -> None:
    tok = _login(client)
    snap = _snap(client, env)
    with env.sf() as s, s.begin():
        if status == "draft":  # a confirmed draft: its MERGE job is waiting
            s.add(
                Job(
                    kind=JobKind.MERGE,
                    payload={"plan_id": 1},
                    status=JobState.QUEUED,
                    run_after=datetime.now(UTC),
                )
            )
        s.add(
            Plan(
                kind="merge",
                release_group_mbid="g",
                canonical_release_mbid="r",
                status=status,
                ops=[{"op": "move", "track_id": env.track, "old": {"path": "x"}, "new_path": "y"}],
            )
        )
    r = client.post(env.url, data={"csrf_token": tok, "snapshot_id": snap})
    assert r.status_code == 409 and env.title() == "Imported"
