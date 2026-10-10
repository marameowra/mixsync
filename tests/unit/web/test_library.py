import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.core.jobs import JobKind
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
    assert G in html and f"{R1} (1)" in html and "Preview merge" in html
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
