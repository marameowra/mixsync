import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.requests import create_request
from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.core.jobs import JobState
from mixsync.core.matching import Band, MatchResult, Penalty
from mixsync.db import decisions
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.auth import Role, User, UserRole
from mixsync.db.models.work import Job
from mixsync.db.models.work import Request as AlbumRequest
from mixsync.web.routes import requests as requests_route
from mixsync.web.routes.review import parse_release_mbid
from mixsync.web.templating import duration, filesize

MBID = "22222222-2222-2222-2222-222222222222"
AUDIO = b"0123456789" * 100


@pytest.fixture
def sf(settings: Settings) -> sessionmaker[Session]:
    upgrade()
    return make_session_factory(make_engine(settings))


@pytest.fixture
def downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "downloads"
    d.mkdir()
    monkeypatch.setenv("MIXSYNC_DOWNLOADS_DIR", str(d))
    monkeypatch.setenv("MIXSYNC_DATA_DIR", str(tmp_path / "data"))
    (d / "a.mp3").write_bytes(AUDIO)
    return d


def _make_decision(
    sf: sessionmaker[Session], user: str, local_path: str, reason: str = "title differs"
) -> int:
    with sf() as s, s.begin():
        uid = s.query(User).filter_by(username=user).one().id
        res = MatchResult(0.2, Band.REVIEW, (Penalty("title", 0.5, 2.0, reason),), ("v<b>",), 1)
        evidence = {
            "source": {
                "peer": "peer1",
                "folder": "Music\\Album",
                "files": [{"local_path": local_path, "name": "a.mp3", "format": "mp3"}],
            },
            "target": {"title": "Album", "artist": "Artist", "tracks": []},
            "acoustid": [{"score": 0.97, "recording_ids": ["r1"]}],
            "duration_deltas": [1.5],
        }
        return decisions.record(
            s, res, evidence=evidence, user_id=uid, stage=2, release_mbid=MBID
        ).id


@pytest.fixture
def client(sf: sessionmaker[Session], downloads: Path) -> Iterator[TestClient]:
    with sf() as s, s.begin():
        create_user(s, "alice", "pw")
        create_user(s, "bob", "pw")
        create_user(s, "root", "pw", admin=True)
        create_user(s, "nocap", "pw")
        role = Role(name="none")
        s.add(role)
        s.flush()
        uid = s.query(User).filter_by(username="nocap").one().id
        s.execute(delete(UserRole).where(UserRole.user_id == uid))
        s.add(UserRole(user_id=uid, role_id=role.id))
    with TestClient(create_app(sf, Settings()), follow_redirects=False) as c:
        yield c


def _csrf(c: TestClient) -> str:
    m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', c.get("/").text)
    assert m
    return m.group(1)


def _login(c: TestClient, user: str) -> str:
    c.cookies.clear()
    assert c.post("/login", data={"username": user, "password": "pw"}).status_code == 303
    return _csrf(c)


def test_nocap_rejected_everywhere(client: TestClient, sf: sessionmaker[Session]) -> None:
    i = _make_decision(sf, "alice", "/x")
    tok = _login(client, "nocap")
    for path in ["/review", f"/review/{i}", f"/media/preview/{i}/0"]:
        assert client.get(path).status_code == 403
    for action in ["accept", "reject", "repick"]:
        r = client.post(f"/review/{i}/{action}", data={"csrf_token": tok, "release": MBID})
        assert r.status_code == 403


def test_non_owner_403_approver_ok(client: TestClient, sf: sessionmaker[Session]) -> None:
    i = _make_decision(sf, "alice", "/x")
    tok = _login(client, "bob")
    assert client.get(f"/review/{i}").status_code == 403
    assert client.get(f"/media/preview/{i}/0").status_code == 403
    assert client.post(f"/review/{i}/accept", data={"csrf_token": tok}).status_code == 403
    assert f'href="/review/{i}"' not in client.get("/review").text
    _login(client, "root")
    assert f'href="/review/{i}"' in client.get("/review").text
    assert client.get(f"/review/{i}").status_code == 200


def test_list_and_home_count(client: TestClient, sf: sessionmaker[Session]) -> None:
    i = _make_decision(sf, "alice", "/x")
    _login(client, "alice")
    assert f'href="/review/{i}"' in client.get("/review").text
    assert "1 item awaiting review" in client.get("/").text


def test_detail_reasons_verbatim_and_escaped(client: TestClient, sf: sessionmaker[Session]) -> None:
    i = _make_decision(sf, "alice", "/x", reason="<script>alert(1)</script> & more")
    _login(client, "alice")
    html = client.get(f"/review/{i}").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; more" in html
    assert "<script>" not in html and "v&lt;b&gt;" in html
    assert "<audio controls" in html and f"/media/preview/{i}/0" in html
    assert "peer1" in html and "0.97" in html


@pytest.mark.parametrize(
    ("action", "status"), [("accept", "accepted"), ("reject", "rejected"), ("repick", "repick")]
)
def test_actions(client: TestClient, sf: sessionmaker[Session], action: str, status: str) -> None:
    i = _make_decision(sf, "alice", "/x")
    tok = _login(client, "alice")
    r = client.post(f"/review/{i}/{action}", data={"csrf_token": tok, "release": MBID})
    assert r.status_code == 303 and r.headers["location"] == "/review"
    with sf() as s:
        d = decisions.get(s, i)
        assert d and d.status == status
    # double submit: error, state unchanged
    r = client.post(f"/review/{i}/reject", data={"csrf_token": tok})
    assert r.status_code == 409
    with sf() as s:
        d = decisions.get(s, i)
        assert d and d.status == status


def test_csrf_missing_invalid_valid(client: TestClient, sf: sessionmaker[Session]) -> None:
    i = _make_decision(sf, "alice", "/x")
    tok = _login(client, "alice")
    for action in ["accept", "reject", "repick"]:
        url = f"/review/{i}/{action}"
        assert client.post(url, data={"release": MBID}).status_code == 403
        assert client.post(url, data={"csrf_token": "bad", "release": MBID}).status_code == 403
    assert client.post("/logout").status_code == 403
    r = client.post(f"/review/{i}/accept", headers={"X-CSRF-Token": tok})
    assert r.status_code == 303
    assert client.post("/logout", data={"csrf_token": tok}).status_code == 303


def test_csrf_token_is_per_session(client: TestClient) -> None:
    assert _login(client, "alice") != _login(client, "root")


def test_login_needs_no_csrf(client: TestClient) -> None:
    assert client.post("/login", data={"username": "alice", "password": "pw"}).status_code == 303


@pytest.mark.parametrize(
    "text",
    [
        MBID,
        MBID.upper(),
        f"https://musicbrainz.org/release/{MBID}",
        f"https://musicbrainz.org/release/{MBID}/cover-art?x=1",
        f" http://www.musicbrainz.org/release/{MBID} ",
    ],
)
def test_parse_mbid_ok(text: str) -> None:
    assert parse_release_mbid(text) == MBID


@pytest.mark.parametrize(
    "text",
    [
        "",
        "junk",
        "1234",
        "https://evil.example/release/" + MBID,
        "z" * 36,
        f"https://musicbrainz.org/artist/{MBID}",
    ],
)
def test_repick_rejects_junk(client: TestClient, sf: sessionmaker[Session], text: str) -> None:
    i = _make_decision(sf, "alice", "/x")
    tok = _login(client, "alice")
    r = client.post(f"/review/{i}/repick", data={"csrf_token": tok, "release": text})
    assert r.status_code in (400, 422)
    with sf() as s:
        d = decisions.get(s, i)
        assert d and d.status == "pending_review"


def test_media_range_206(client: TestClient, sf: sessionmaker[Session], downloads: Path) -> None:
    i = _make_decision(sf, "alice", str(downloads / "a.mp3"))
    _login(client, "alice")
    r = client.get(f"/media/preview/{i}/0", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == AUDIO[:10]
    assert client.get(f"/media/preview/{i}/0").content == AUDIO
    assert client.get(f"/media/preview/{i}/5").status_code == 404


def test_media_out_of_root_404(
    client: TestClient, sf: sessionmaker[Session], downloads: Path, tmp_path: Path
) -> None:
    (tmp_path / "secret.mp3").write_bytes(b"s")
    (downloads / "link.mp3").symlink_to(tmp_path / "secret.mp3")
    _login(client, "alice")
    for p in [
        str(downloads / ".." / "secret.mp3"),
        str(tmp_path / "secret.mp3"),
        str(downloads / "link.mp3"),
        str(downloads / "missing.mp3"),
        "/etc/passwd",
        "",
    ]:
        i = _make_decision(sf, "alice", p)
        assert client.get(f"/media/preview/{i}/0").status_code == 404, p


def test_static_css(client: TestClient) -> None:
    assert client.get("/static/app.css").status_code == 200


def test_filters() -> None:
    assert [filesize(x) for x in (0, 30_300_000, 2_500_000_000, None)] == [
        "0.0 MB",
        "30.3 MB",
        "2.5 GB",
        "–",
    ]
    assert [duration(x) for x in (0, 59, 60, 251, 3600, 3661, None)] == [
        "0:00",
        "0:59",
        "1:00",
        "4:11",
        "1:00:00",
        "1:01:01",
        "–",
    ]


def test_errors_render_html(client: TestClient, sf: sessionmaker[Session]) -> None:
    i = _make_decision(sf, "alice", "/x")
    tok = _login(client, "alice")
    client.post(f"/review/{i}/accept", data={"csrf_token": tok})
    r = client.post(f"/review/{i}/reject", data={"csrf_token": tok})
    assert r.status_code == 409 and "text/html" in r.headers["content-type"]
    assert "not pending review" in r.text and 'href="/review"' in r.text
    _login(client, "bob")
    r = client.get(f"/review/{i}")
    assert r.status_code == 403 and "missing capability: approve" in r.text


def test_unauthenticated_still_redirects(client: TestClient) -> None:
    r = client.get("/review")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def _req(sf: sessionmaker[Session], user: str, status: str, job: JobState | None) -> int:
    with sf() as s, s.begin():
        uid = s.query(User).filter_by(username=user).one().id
        req = create_request(s, datetime.now(UTC), uid, MBID)
        req.status = status
        j = s.scalars(select(Job).order_by(Job.id.desc())).first()
        assert j
        if job is None:
            s.delete(j)
        else:
            j.status = job
        return req.id


def test_owner_deletes_failed_request(client: TestClient, sf: sessionmaker[Session]) -> None:
    rid = _req(sf, "alice", "failed", JobState.FAILED)
    did = _make_decision(sf, "alice", "/x")
    with sf() as s, s.begin():
        d = decisions.get(s, did)
        assert d
        d.evidence = {**d.evidence, "request_id": rid}
    tok = _login(client, "alice")
    assert f"/requests/{rid}/delete" in client.get("/requests").text
    r = client.post(f"/requests/{rid}/delete", data={"csrf_token": tok})
    assert r.status_code == 303
    with sf() as s:
        assert s.get(AlbumRequest, rid) is None and not s.scalars(select(Job)).all()
        d = decisions.get(s, did)
        assert d and d.status == "rejected"  # kept for the record, off the review queue


def test_delete_request_with_stage1_pending_decision(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    rid = _req(sf, "alice", "failed", JobState.FAILED)
    did = _make_decision(sf, "alice", "/x")
    with sf() as s, s.begin():
        d = decisions.get(s, did)
        assert d
        d.stage = 1  # the pipeline's download pick, not a file review
        d.evidence = {**d.evidence, "request_id": rid}
    tok = _login(client, "alice")
    r = client.post(f"/requests/{rid}/delete", data={"csrf_token": tok})
    assert r.status_code == 303
    with sf() as s:
        assert s.get(AlbumRequest, rid) is None


def test_delete_request_refused(client: TestClient, sf: sessionmaker[Session]) -> None:
    mine = _req(sf, "alice", "failed", None)
    busy = _req(sf, "alice", "searching", JobState.RUNNING)
    queued = _req(sf, "alice", "searching", JobState.QUEUED)
    tok = _login(client, "bob")
    assert client.post(f"/requests/{mine}/delete", data={"csrf_token": tok}).status_code == 403
    tok = _login(client, "alice")
    assert client.post(f"/requests/{mine}/delete").status_code == 403  # no CSRF token
    for rid in (busy, queued):
        assert f"/requests/{rid}/delete" not in client.get("/requests").text
        assert client.post(f"/requests/{rid}/delete", data={"csrf_token": tok}).status_code == 409
    assert client.post("/requests/999/delete", data={"csrf_token": tok}).status_code == 404


def test_delete_no_results_request(client: TestClient, sf: sessionmaker[Session]) -> None:
    rid = _req(sf, "alice", "no_results", JobState.QUEUED)
    tok = _login(client, "alice")
    assert f"/requests/{rid}/delete" in client.get("/requests").text
    assert client.post(f"/requests/{rid}/delete", data={"csrf_token": tok}).status_code == 303
    with sf() as s:
        assert s.get(AlbumRequest, rid) is None and not s.scalars(select(Job)).all()


@pytest.mark.parametrize("state", [JobState.LEASED, JobState.RUNNING])
def test_delete_no_results_request_refused_while_searching(
    client: TestClient, sf: sessionmaker[Session], state: JobState
) -> None:
    rid = _req(sf, "alice", "no_results", state)
    tok = _login(client, "alice")
    assert f"/requests/{rid}/delete" not in client.get("/requests").text
    assert client.post(f"/requests/{rid}/delete", data={"csrf_token": tok}).status_code == 409


def test_delete_request_409_when_review_acts_first(
    client: TestClient, sf: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _req(sf, "alice", "failed", JobState.FAILED)
    did = _make_decision(sf, "alice", "/x")
    with sf() as s, s.begin():
        d = decisions.get(s, did)
        assert d
        d.evidence = {**d.evidence, "request_id": rid}

    def raced(*_a: object, **_k: object) -> None:
        raise decisions.DecisionStateError("already acted on")

    monkeypatch.setattr(decisions, "act", raced)
    tok = _login(client, "alice")
    assert client.post(f"/requests/{rid}/delete", data={"csrf_token": tok}).status_code == 409
    with sf() as s:
        assert s.get(AlbumRequest, rid) is not None


def test_delete_request_409_when_job_claimed_after_check(
    client: TestClient, sf: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _req(sf, "alice", "no_results", JobState.LEASED)  # a worker claimed it after the check
    monkeypatch.setattr(requests_route, "_deletable", lambda *_a: True)
    tok = _login(client, "alice")
    assert client.post(f"/requests/{rid}/delete", data={"csrf_token": tok}).status_code == 409
    with sf() as s:
        assert s.get(AlbumRequest, rid) is not None
        assert s.scalars(select(Job.status)).all() == [JobState.LEASED]



def test_delete_request_409_rolls_back_decision_rejects(
    client: TestClient, sf: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _req(sf, "alice", "failed", JobState.FAILED)
    dids = [_make_decision(sf, "alice", "/x"), _make_decision(sf, "alice", "/y")]
    with sf() as s, s.begin():
        for did in dids:
            d = decisions.get(s, did)
            assert d
            d.evidence = {**d.evidence, "request_id": rid}
    real, calls = decisions.act, []

    def second_raises(*a: object, **k: object) -> object:
        calls.append(a)
        if len(calls) == 2:
            raise decisions.DecisionStateError("already acted on")
        return real(*a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(decisions, "act", second_raises)
    tok = _login(client, "alice")
    assert client.post(f"/requests/{rid}/delete", data={"csrf_token": tok}).status_code == 409
    with sf() as s:
        assert [decisions.get(s, i).status for i in dids] == [decisions.PENDING] * 2  # type: ignore[union-attr]
