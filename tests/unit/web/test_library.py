import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.merge import PlanInProgress
from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.core.errors import TransientError
from mixsync.core.jobs import JobKind, JobState
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.library import Plan, Track
from mixsync.db.models.work import Job

G = "11111111-1111-1111-1111-111111111111"
R1 = "11111111-1111-1111-1111-111111111112"
R2 = "11111111-1111-1111-1111-111111111113"


class FakeMerger:
    def __init__(self, sf: sessionmaker[Session]) -> None:
        self.sf = sf

    async def build(self, group: str) -> int:
        if group == "busy":
            raise PlanInProgress("plan 1 is running")
        if group == "mb":
            raise TransientError("musicbrainz still refusing")
        if group == "missing":
            raise LookupError("no verified tracks")
        return _plan(self.sf, "draft", [_op("move", 0)])


def _op(kind: str, i: int) -> dict[str, Any]:
    old = {"path": f"a/{i}.flac", "release_mbid": R1}
    if kind == "move":
        tags = {"release_id": R2, "disc": 1, "track": 1, "title": "T", "album": "A"}
        return {"op": kind, "old": old, "new_path": f"b/{i}.flac", "tags": tags, "done": False}
    if kind == "trash":
        return {"op": kind, "old": old, "keeper": f"k/{i}.flac", "done": False}
    return {"op": kind, "old": old, "done": False}


def _plan(
    sf: sessionmaker[Session], status: str, ops: list[dict[str, Any]], error: str | None = None
) -> int:
    with sf() as s, s.begin():
        p = Plan(
            kind="merge",
            release_group_mbid=G,
            canonical_release_mbid=R2,
            ops=ops,
            status=status,
            error=error,
        )
        s.add(p)
        s.flush()
        return p.id


@pytest.fixture
def sf(settings: Settings) -> sessionmaker[Session]:
    upgrade()
    return make_session_factory(make_engine(settings))


class NoNames:
    """No MusicBrainz in these tests: every name lookup misses."""

    async def label(self, kind: str, mbid: str, *, cached_only: bool = False) -> str | None:
        return None


@pytest.fixture
def client(sf: sessionmaker[Session]) -> Iterator[TestClient]:
    with sf() as s, s.begin():
        create_user(s, "alice", "pw")
        create_user(s, "root", "pw", admin=True)
        for i, rel in enumerate((R1, R2)):
            s.add(
                Track(
                    path=f"t{i}.flac",
                    release_mbid=rel,
                    release_group_mbid=G,
                    status="verified",
                    batch_id="b",
                    imported_at=datetime.now(UTC),
                )
            )
    app = create_app(sf, Settings())
    app.state.merger = FakeMerger(sf)
    app.state.namer = NoNames()
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _login(c: TestClient, user: str) -> str:
    c.cookies.clear()
    assert c.post("/login", data={"username": user, "password": "pw"}).status_code == 303
    m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', c.get("/").text)
    assert m
    return m.group(1)


def _jobs(sf: sessionmaker[Session]) -> list[Job]:
    with sf() as s:
        return list(s.scalars(select(Job).where(Job.kind.in_([JobKind.MERGE, JobKind.UNDO_MERGE]))))


def test_list_page(client: TestClient, sf: sessionmaker[Session]) -> None:
    pid = _plan(sf, "done", [_op("move", 0)])
    _login(client, "root")
    html = client.get("/library/fragments").text
    assert f'title="{G}">Release group 11111111</a>' in html  # unknown: short id, full id on hover
    assert f'title="{R1}">Release 11111111</a> (1)' in html and "Preview merge" in html
    assert f"/library/plans/{pid}/undo" in html


def test_build_and_preview(client: TestClient) -> None:
    tok = _login(client, "root")
    r = client.post("/library/fragments/plan", data={"csrf_token": tok, "group": G})
    assert r.status_code == 303
    html = client.get(r.headers["location"]).text
    assert "a/0.flac" in html and "b/0.flac" in html and "1 to move" in html
    assert "Confirm merge" in html
    missing = client.post("/library/fragments/plan", data={"csrf_token": tok, "group": "missing"})
    assert missing.status_code == 404


def test_preview_ops_and_error(client: TestClient, sf: sessionmaker[Session]) -> None:
    pid = _plan(sf, "failed", [_op("trash", 1), _op("keep", 2)], error="boom <b>")
    _login(client, "root")
    html = client.get(f"/library/plans/{pid}").text
    assert "k/1.flac" in html and "a/2.flac" in html and "boom &lt;b&gt;" in html
    assert "Undo merge" in html


def test_confirm_enqueues(client: TestClient, sf: sessionmaker[Session]) -> None:
    pid = _plan(sf, "draft", [_op("move", 0)])
    tok = _login(client, "root")
    r = client.post(f"/library/plans/{pid}/confirm", data={"csrf_token": tok})
    assert r.status_code == 303
    assert [j.kind for j in _jobs(sf)] == [JobKind.MERGE]


@pytest.mark.parametrize("action", ["confirm", "undo"])
def test_typed_count(client: TestClient, sf: sessionmaker[Session], action: str) -> None:
    pid = _plan(sf, "draft" if action == "confirm" else "done", [_op("move", i) for i in range(51)])
    tok = _login(client, "root")
    url = f"/library/plans/{pid}/{action}"
    assert "Type 51 to confirm" in client.get(f"/library/plans/{pid}").text
    assert client.post(url, data={"csrf_token": tok}).status_code == 400
    assert client.post(url, data={"csrf_token": tok, "count": "50"}).status_code == 400
    assert _jobs(sf) == []
    assert client.post(url, data={"csrf_token": tok, "count": "51"}).status_code == 303
    assert len(_jobs(sf)) == 1


def test_fifty_needs_no_count(client: TestClient, sf: sessionmaker[Session]) -> None:
    pid = _plan(sf, "draft", [_op("move", i) for i in range(50)] + [_op("keep", 99)])
    tok = _login(client, "root")
    assert client.post(f"/library/plans/{pid}/confirm", data={"csrf_token": tok}).status_code == 303


@pytest.mark.parametrize(
    ("action", "status"),
    [("confirm", "done"), ("confirm", "running"), ("undo", "draft"), ("undo", "undone")],
)
def test_wrong_state_409(
    client: TestClient, sf: sessionmaker[Session], action: str, status: str
) -> None:
    pid = _plan(sf, status, [_op("move", 0)])
    tok = _login(client, "root")
    r = client.post(f"/library/plans/{pid}/{action}", data={"csrf_token": tok})
    assert r.status_code == 409
    assert _jobs(sf) == []


def test_undo_failed_plan(client: TestClient, sf: sessionmaker[Session]) -> None:
    pid = _plan(sf, "failed", [_op("move", 0)])
    tok = _login(client, "root")
    assert client.post(f"/library/plans/{pid}/undo", data={"csrf_token": tok}).status_code == 303
    assert [j.kind for j in _jobs(sf)] == [JobKind.UNDO_MERGE]


def test_csrf_required(client: TestClient, sf: sessionmaker[Session]) -> None:
    pid = _plan(sf, "draft", [_op("move", 0)])
    _login(client, "root")
    assert client.post(f"/library/plans/{pid}/confirm").status_code == 403
    assert client.post("/library/fragments/plan", data={"group": G}).status_code == 403
    assert _jobs(sf) == []


def test_non_admin_forbidden(client: TestClient, sf: sessionmaker[Session]) -> None:
    pid = _plan(sf, "draft", [_op("move", 0)])
    tok = _login(client, "alice")
    assert client.get("/library/fragments").status_code == 403
    assert client.get(f"/library/plans/{pid}").status_code == 403
    for url, data in [
        (f"/library/plans/{pid}/confirm", {}),
        (f"/library/plans/{pid}/undo", {}),
        ("/library/fragments/plan", {"group": G}),
    ]:
        assert client.post(url, data={"csrf_token": tok, **data}).status_code == 403
    assert _jobs(sf) == []


def test_build_errors(client: TestClient) -> None:
    tok = _login(client, "root")
    for group, code in [("busy", 409), ("mb", 503)]:
        r = client.post("/library/fragments/plan", data={"csrf_token": tok, "group": group})
        assert r.status_code == code


def _live_job(sf: sessionmaker[Session], kind: JobKind, pid: int, state: JobState) -> None:
    with sf() as s, s.begin():
        s.add(
            Job(
                kind=kind,
                payload={"plan_id": pid},
                status=state,
                run_after=datetime.now(UTC),
            )
        )


def test_resume_failed(client: TestClient, sf: sessionmaker[Session]) -> None:
    pid = _plan(sf, "failed", [_op("move", 0)], error="x is gone")
    tok = _login(client, "root")
    assert "Resume merge" in client.get(f"/library/plans/{pid}").text
    assert client.post(f"/library/plans/{pid}/resume", data={"csrf_token": tok}).status_code == 303
    assert [j.kind for j in _jobs(sf)] == [JobKind.MERGE]


def test_resume_count_required(client: TestClient, sf: sessionmaker[Session]) -> None:
    pid = _plan(sf, "failed", [_op("move", i) for i in range(51)], error="x")
    tok = _login(client, "root")
    assert client.post(f"/library/plans/{pid}/resume", data={"csrf_token": tok}).status_code == 400
    r = client.post(f"/library/plans/{pid}/resume", data={"csrf_token": tok, "count": "51"})
    assert r.status_code == 303


@pytest.mark.parametrize("status", ["draft", "done", "running", "discarded"])
def test_resume_wrong_state_409(client: TestClient, sf: sessionmaker[Session], status: str) -> None:
    pid = _plan(sf, status, [_op("move", 0)])
    tok = _login(client, "root")
    assert client.post(f"/library/plans/{pid}/resume", data={"csrf_token": tok}).status_code == 409


def test_failed_undo_error_cannot_resume(client: TestClient, sf: sessionmaker[Session]) -> None:
    pid = _plan(sf, "failed", [_op("move", 0)], error="undo: row changed")
    tok = _login(client, "root")
    assert "Resume merge" not in client.get(f"/library/plans/{pid}").text
    assert client.post(f"/library/plans/{pid}/resume", data={"csrf_token": tok}).status_code == 409
    assert client.post(f"/library/plans/{pid}/undo", data={"csrf_token": tok}).status_code == 303


def test_a_precondition_failure_offers_only_undo(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    pid = _plan(sf, "failed", [_op("move", 0)], error="blocked: x is gone")
    _login(client, "root")
    html = client.get(f"/library/plans/{pid}").text
    assert "Resume merge" not in html and "Undo merge" in html and "x is gone" in html


def test_fragments_always_lists_stuck_plans(client: TestClient, sf: sessionmaker[Session]) -> None:
    old = _plan(sf, "failed", [_op("move", 0)], error="boom")
    for _ in range(21):
        _plan(sf, "done", [_op("move", 0)])
    _login(client, "root")
    assert f'/library/plans/{old}"' in client.get("/library/fragments").text


@pytest.mark.parametrize("status", ["running", "undoing"])
def test_undo_stuck_plan_only_without_live_job(
    client: TestClient, sf: sessionmaker[Session], status: str
) -> None:
    pid = _plan(sf, status, [_op("move", 0)])
    tok = _login(client, "root")
    _live_job(sf, JobKind.MERGE, pid, JobState.RUNNING)
    assert client.post(f"/library/plans/{pid}/undo", data={"csrf_token": tok}).status_code == 409
    assert "Undo merge" not in client.get(f"/library/plans/{pid}").text
    with sf() as s, s.begin():
        s.execute(Job.__table__.delete())
    assert client.post(f"/library/plans/{pid}/undo", data={"csrf_token": tok}).status_code == 303


def test_resume_blocked_by_live_job(client: TestClient, sf: sessionmaker[Session]) -> None:
    pid = _plan(sf, "failed", [_op("move", 0)], error="x")
    tok = _login(client, "root")
    _live_job(sf, JobKind.MERGE, pid, JobState.QUEUED)
    assert client.post(f"/library/plans/{pid}/resume", data={"csrf_token": tok}).status_code == 409


def test_discarded_is_terminal(client: TestClient, sf: sessionmaker[Session]) -> None:
    pid = _plan(sf, "discarded", [_op("move", 0)])
    tok = _login(client, "root")
    assert "discarded" in client.get("/library/fragments").text
    html = client.get(f"/library/plans/{pid}").text
    assert "<button" not in html.split("<main>")[1]
    for action in ("confirm", "undo"):
        r = client.post(f"/library/plans/{pid}/{action}", data={"csrf_token": tok})
        assert r.status_code == 409


def _aged_plan(sf: sessionmaker[Session], status: str, days: float) -> int:
    with sf() as s, s.begin():
        p = Plan(
            kind="merge",
            release_group_mbid=G,
            canonical_release_mbid=R2,
            ops=[_op("move", 0)],
            status=status,
            updated_at=datetime.now(UTC) - timedelta(days=days),
        )
        s.add(p)
        s.flush()
        return p.id


def test_a_failed_plan_stays_undoable_past_the_window(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    old = _aged_plan(sf, "failed", 90)  # its original may exist only in the trash
    tok = _login(client, "root")
    assert f"/library/plans/{old}/undo" in client.get(f"/library/plans/{old}").text
    assert client.post(f"/library/plans/{old}/undo", data={"csrf_token": tok}).status_code == 303


def test_undo_is_offered_only_within_the_window(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    status = "done"
    fresh = _aged_plan(sf, status, 29)
    stale = _aged_plan(sf, status, 31)
    tok = _login(client, "root")
    html = client.get("/library/fragments").text
    assert f"/library/plans/{fresh}/undo" in html
    assert f"/library/plans/{stale}/undo" not in html
    assert f"/library/plans/{fresh}/undo" in client.get(f"/library/plans/{fresh}").text
    assert f"/library/plans/{stale}/undo" not in client.get(f"/library/plans/{stale}").text
    assert client.post(f"/library/plans/{stale}/undo", data={"csrf_token": tok}).status_code == 409
    assert client.post(f"/library/plans/{fresh}/undo", data={"csrf_token": tok}).status_code == 303
