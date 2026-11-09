"""The admin scan for done requests whose release still lacks tracks (#370): the routes on the
settings page and the MISSING_SCAN job, run by the worker's run_job."""

from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from mixsync.acquire.missing_scan import KIND, MissingScan
from mixsync.core.errors import TransientError
from mixsync.core.jobs import JobKind, JobState
from mixsync.core.matching import AlbumInfo
from mixsync.db.auth import create_user
from mixsync.db.models.library import Plan, Track
from mixsync.db.models.work import Job, Request

from .conftest import Env, login
from .fakes import make_album

TRI = make_album("Tri", "tri-release", ("Uno", "Dos", "Tres"))  # Tri-rec-1..3, group Tri-rg
DUO = make_album("Duo", "duo-release", ("Left", "Right"))
SOLO = make_album("Solo", "solo-release", ("Only",))
ALBUMS = {a.release_id or "": a for a in (TRI, DUO, SOLO)}
SCAN = "/settings/missing-tracks"


class Metadata:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = 0  # this many calls raise TransientError first

    async def get_release(self, mbid: str, *, fresh: bool = False) -> AlbumInfo:
        self.calls.append(mbid)
        if self.fail:
            self.fail -= 1
            raise TransientError("musicbrainz 503")
        return ALBUMS[mbid]


def _setup(env: Env) -> Metadata:
    meta = Metadata()
    env.handlers = env.handlers | MissingScan(env.sessions, env.clock, meta).handlers()  # type: ignore[arg-type]
    with env.sessions.begin() as s:
        create_user(s, "root", "pw", admin=True)
    return meta


def _request(env: Env, album: AlbumInfo, status: str, **kw: Any) -> int:
    with env.sessions.begin() as s:
        req = Request(user_id=env.user_id, release_mbid=album.release_id, status=status, **kw)
        s.add(req)
        s.flush()
        return req.id


def _have(env: Env, album: AlbumInfo, *nums: int) -> None:
    """Put these tracks (1-based) of the album in the library."""
    with env.sessions.begin() as s:
        for n in nums:
            s.add(
                Track(
                    path=f"{album.album}/{n}.flac",
                    recording_mbid=album.tracks[n - 1].recording_id,
                    release_mbid=album.release_id,
                    release_group_mbid=album.release_group_id,
                    status="verified",
                    batch_id="b",
                    imported_at=datetime.now(UTC),
                )
            )


def _deleted(env: Env, album: AlbumInfo, n: int, status: str) -> None:
    """A library delete plan that trashed track n of the album."""
    old = {
        "path": f"{album.album}/{n}.flac",
        "recording_mbid": album.tracks[n - 1].recording_id,
        "release_group_mbid": album.release_group_id,
    }
    with env.sessions.begin() as s:
        s.add(
            Plan(
                kind="delete",
                release_group_mbid=album.release_group_id,
                canonical_release_mbid="",
                status=status,
                ops=[{"op": "trash", "keeper": None, "track_id": 99, "old": old, "done": True}],
            )
        )


def _state(env: Env) -> list[tuple[Any, ...]]:
    with env.sessions() as s:
        reqs = s.scalars(select(Request).order_by(Request.id))
        return [(r.id, r.status, r.partial_note, r.last_error) for r in reqs]


def _searches(env: Env) -> list[int]:
    return sorted(j.payload["request_id"] for j in env.jobs(JobKind.SEARCH))


async def _scan(env: Env, web: TestClient, tok: str) -> None:
    """Press the button, see the scan in progress, then run it."""
    assert web.post(f"{SCAN}/scan", data={"csrf_token": tok}).status_code == 303
    assert "Scanning" in web.get(SCAN).text
    job = await env.step()
    assert job is not None and job.kind == JobKind.MISSING_SCAN


def _draft(env: Env) -> Plan | None:
    with env.sessions() as s:
        return s.scalars(select(Plan).where(Plan.kind == KIND).order_by(Plan.id.desc())).first()


def _listed(env: Env) -> list[int]:
    plan = _draft(env)
    return [op["request_id"] for op in plan.ops] if plan else []


def test_admin_only(env: Env, web: TestClient) -> None:
    _setup(env)
    with env.sessions.begin() as s:
        s.add(Plan(kind=KIND, release_group_mbid="", canonical_release_mbid="", ops=[]))
    pid = _draft(env).id  # type: ignore[union-attr]
    tok = login(web, "alice")
    assert web.get(SCAN).status_code == 403
    for url in (f"{SCAN}/scan", f"{SCAN}/{pid}/confirm", f"{SCAN}/{pid}/discard"):
        assert web.post(url, data={"csrf_token": tok}).status_code == 403
    assert env.jobs(JobKind.MISSING_SCAN) == [] and _draft(env).status == "draft"  # type: ignore[union-attr]
    tok = login(web, "root")
    assert "Scan requests for missing tracks" in web.get("/settings").text
    assert web.post(f"{SCAN}/scan").status_code == 403  # no CSRF token
    assert env.jobs(JobKind.MISSING_SCAN) == []


def test_pressing_twice_queues_one_scan(env: Env, web: TestClient) -> None:
    _setup(env)
    tok = login(web, "root")
    for _ in range(2):
        assert web.post(f"{SCAN}/scan", data={"csrf_token": tok}).status_code == 303
    assert len(env.jobs(JobKind.MISSING_SCAN)) == 1


async def test_preview_lists_requests_with_gaps_and_changes_nothing(
    env: Env, web: TestClient
) -> None:
    meta = _setup(env)
    _have(env, TRI, 1, 2)
    _have(env, DUO, 1)
    _have(env, SOLO, 1)
    tri = _request(env, TRI, "imported")
    duo = _request(env, DUO, "owned")
    _request(env, SOLO, "imported")  # complete
    _request(env, TRI, "imported", provisional=True)
    _request(env, TRI, "imported", source="drop")  # from files
    for status in ("unverified", "failed", "searching", "partial", "downloading", "review"):
        _request(env, TRI, status)
    before = _state(env)
    tok = login(web, "root")
    await _scan(env, web, tok)
    assert _state(env) == before  # the scan is a dry run
    assert _searches(env) == []
    assert meta.calls == ["tri-release", "duo-release", "solo-release"]
    assert _listed(env) == [tri, duo]
    page = web.get(SCAN).text
    assert "Tri Band" in page and "Tri Album" in page and "imported" in page
    assert "2 of 3 tracks present" in page and "Tres" in page
    assert "Duo Album" in page and "owned" in page and "1 of 2 tracks present" in page
    assert "Right" in page and "Solo Album" not in page
    assert "Confirm" in page and "Discard" in page and "Scanning" not in page


async def test_a_recording_deleted_on_purpose_is_not_missing_unless_undone(
    env: Env, web: TestClient
) -> None:
    _setup(env)
    _have(env, TRI, 1, 2)
    _have(env, DUO, 1)
    _deleted(env, TRI, 3, "done")
    _deleted(env, DUO, 2, "undone")
    _request(env, TRI, "imported")
    duo = _request(env, DUO, "imported")
    await _scan(env, web, login(web, "root"))
    assert _listed(env) == [duo]


async def test_confirm_reopens_unchanged_requests_and_queues_one_search_each(
    env: Env, web: TestClient
) -> None:
    _setup(env)
    _have(env, TRI, 1, 2)
    tri = _request(env, TRI, "imported")
    duo = _request(env, DUO, "owned")
    moved = _request(env, DUO, "imported")
    gone = _request(env, DUO, "imported")
    tok = login(web, "root")
    await _scan(env, web, tok)
    assert _listed(env) == [tri, duo, moved, gone]
    with env.sessions.begin() as s:
        s.get_one(Request, moved).status = "failed"  # changed since the scan
        s.delete(s.get_one(Request, gone))
    pid = _draft(env).id  # type: ignore[union-attr]
    assert web.post(f"{SCAN}/{pid}/confirm", data={"csrf_token": tok}).status_code == 303
    assert web.post(f"{SCAN}/{pid}/confirm", data={"csrf_token": tok}).status_code == 409
    with env.sessions() as s:
        t, d, m = (s.get_one(Request, i) for i in (tri, duo, moved))
        assert (t.status, t.partial_note) == ("partial", "2 of 3 tracks present")
        assert t.last_error == "partial: 2 of 3 tracks present; found by the missing-track scan"
        assert (d.status, d.partial_note) == ("partial", "0 of 2 tracks present")
        assert (m.status, m.partial_note) == ("failed", None)
    assert _searches(env) == [tri, duo]
    assert "reopened" in web.get(SCAN).text


async def test_discard_changes_nothing(env: Env, web: TestClient) -> None:
    _setup(env)
    _request(env, TRI, "imported")
    tok = login(web, "root")
    await _scan(env, web, tok)
    before = _state(env)
    pid = _draft(env).id  # type: ignore[union-attr]
    assert web.post(f"{SCAN}/{pid}/discard", data={"csrf_token": tok}).status_code == 303
    assert _draft(env).status == "discarded"  # type: ignore[union-attr]
    assert _state(env) == before and _searches(env) == []
    assert web.post(f"{SCAN}/{pid}/confirm", data={"csrf_token": tok}).status_code == 409
    assert _state(env) == before and _searches(env) == []


async def test_nothing_missing_stores_no_draft(env: Env, web: TestClient) -> None:
    _setup(env)
    _have(env, SOLO, 1)
    _request(env, SOLO, "owned")
    await _scan(env, web, login(web, "root"))
    assert _draft(env) is None
    assert "No imported or owned request is missing tracks" in web.get(SCAN).text


async def test_a_musicbrainz_outage_retries_the_scan(env: Env, web: TestClient) -> None:
    meta = _setup(env)
    meta.fail = 1
    tri = _request(env, TRI, "imported")
    await _scan(env, web, login(web, "root"))
    (job,) = env.jobs(JobKind.MISSING_SCAN)
    assert job.status == JobState.RETRY_WAIT and _draft(env) is None
    assert "Scanning" in web.get(SCAN).text
    await env.step()
    assert env.jobs(JobKind.MISSING_SCAN)[0].status == JobState.SUCCEEDED
    assert _listed(env) == [tri]


async def test_library_plan_pages_ignore_the_scan(env: Env, web: TestClient) -> None:
    _setup(env)
    _request(env, TRI, "imported")
    tok = login(web, "root")
    await _scan(env, web, tok)
    pid = _draft(env).id  # type: ignore[union-attr]
    assert f"/library/plans/{pid}" not in web.get("/library/fragments").text
    assert web.get(f"/library/plans/{pid}").status_code == 404
    for action in ("confirm", "discard", "resume", "undo"):
        r = web.post(f"/library/plans/{pid}/{action}", data={"csrf_token": tok})
        assert r.status_code == 404
    with env.sessions() as s:
        assert s.scalars(select(Job).where(Job.kind == JobKind.MERGE)).all() == []
    assert _draft(env).status == "draft"  # type: ignore[union-attr]
