import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.core.matching import Band, MatchResult, Penalty
from mixsync.db import decisions
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.auth import Role, User, UserRole
from mixsync.web.routes.review import parse_release_mbid

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
