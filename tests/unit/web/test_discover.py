import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

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
from mixsync.db.models.accounts import UserLink
from mixsync.db.models.auth import User
from mixsync.db.models.discovery import DiscoveryPlaylist, ProvisionalHold, ProvisionalTrack
from mixsync.db.models.library import Track
from mixsync.db.models.work import Job
from mixsync.db.models.work import Request as WorkRequest

NOW = datetime.now(UTC)
KEYS = ("owned", "waiting", "indiscover", "kept", "expired", "soon")
M = {k: f"{i:08d}-0000-0000-0000-000000000000" for i, k in enumerate(KEYS, 1)}


@pytest.fixture
def sf(settings: Settings) -> sessionmaker[Session]:
    upgrade()
    sf = make_session_factory(make_engine(settings))
    with sf() as s, s.begin():
        for name in ("alice", "bob"):
            create_user(s, name, "pw")
    return sf


@pytest.fixture
def client(sf: sessionmaker[Session]) -> Iterator[TestClient]:
    with TestClient(create_app(sf, Settings()), follow_redirects=False) as c:
        yield c


def _login(c: TestClient, user: str) -> str:
    c.cookies.clear()
    assert c.post("/login", data={"username": user, "password": "pw"}).status_code == 303
    m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', c.get("/discover").text)
    assert m
    return m.group(1)


def _seed(sf: sessionmaker[Session]) -> int:
    """Alice: linked, one playlist with a track in each state."""
    with sf() as s, s.begin():
        uid = s.scalars(select(User.id).where(User.username == "alice")).one()
        for svc in ("listenbrainz", "navidrome"):
            s.add(
                UserLink(
                    user_id=uid,
                    service=svc,
                    external_username="x",
                    secret_encrypted="x",
                    verified_at=NOW,
                )
            )
        pl = DiscoveryPlaylist(
            user_id=uid,
            name="Discovery",
            last_error="boom",
            entries=[
                {"recording_mbid": m, "artist": "Art", "title": f"T-{k}"} for k, m in M.items()
            ],
        )
        s.add(pl)
        s.flush()
        s.add(
            Track(
                path="o.mp3",
                recording_mbid=M["owned"],
                status="verified",
                batch_id="b",
                imported_at=NOW,
            )
        )
        for k, days in (("indiscover", 20), ("kept", 20), ("expired", 20), ("soon", 3)):
            pt = ProvisionalTrack(
                path=f"{k}.mp3",
                recording_mbid=M[k],
                batch_id="b",
                imported_at=NOW,
                title=f"T-{k}",
                artist="Art",
            )
            s.add(pt)
            s.flush()
            s.add(
                ProvisionalHold(
                    user_id=uid,
                    recording_mbid=M[k],
                    playlist_id=pl.id,
                    provisional_track_id=pt.id,
                    expires_at=NOW + timedelta(days=days),
                    kept_at=NOW if k == "kept" else None,
                    expired_at=NOW if k == "expired" else None,
                )
            )
        s.add(ProvisionalHold(user_id=uid, recording_mbid=M["waiting"], playlist_id=pl.id))
        return pl.id


def _status(text: str, key: str) -> str:
    m = re.search(rf"T-{key}</td>\s*<td>([^<(]*)", text)
    assert m
    return m.group(1).strip()


def test_page_shows_each_status_and_expiring(client: TestClient, sf: sessionmaker[Session]) -> None:
    _seed(sf)
    _login(client, "alice")
    text = client.get("/discover").text
    assert [_status(text.split("<h2>Discovery")[1], k) for k in KEYS] == [
        "owned",
        "waiting for download",
        "in discover",
        "kept",
        "expired",
        "in discover",
    ]
    assert "boom" in text and "Build now" in text
    soon = text.split("Expiring in the next 7 days")[1].split("<h2>Discovery")[0]
    assert "T-soon" in soon and "T-indiscover" not in soon  # 20 days out is not listed


def test_keep_marks_hold_and_other_user_gets_404(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    _seed(sf)
    tok = _login(client, "bob")
    r = client.post(f"/discover/keep/{M['indiscover']}", data={"csrf_token": tok})
    assert r.status_code == 404
    tok = _login(client, "alice")
    r = client.post(f"/discover/keep/{M['indiscover']}", data={"csrf_token": tok})
    assert r.status_code == 303
    with sf() as s:
        h = s.scalars(select(ProvisionalHold).filter_by(recording_mbid=M["indiscover"])).one()
        assert h.kept_at is not None and h.keep_reason == "manual"
    assert _status(client.get("/discover").text, "indiscover") == "kept"


def test_settings_validate_and_save(client: TestClient, sf: sessionmaker[Session]) -> None:
    pid = _seed(sf)
    tok = _login(client, "alice")
    good = {
        "size": "40",
        "unowned_ratio": "0.5",
        "cadence_days": "3",
        "retention_days": "10",
        "keep_plays": "2",
        "csrf_token": tok,
    }
    bads = (
        {"size": "0"},
        {"size": "x"},
        {"size": "501"},
        {"unowned_ratio": "1.5"},
        {"unowned_ratio": "nan"},
        {"cadence_days": "-1"},
        {"retention_days": "9999"},
        {"keep_plays": ""},
    )
    for bad in bads:
        assert client.post(f"/discover/{pid}/settings", data=good | bad).status_code == 400
    with sf() as s:
        assert s.get_one(DiscoveryPlaylist, pid).size == 50  # nothing half-saved
    assert client.post(f"/discover/{pid}/settings", data=good).status_code == 303
    with sf() as s:
        pl = s.get_one(DiscoveryPlaylist, pid)
        saved = (pl.size, pl.unowned_ratio, pl.cadence_days, pl.retention_days, pl.keep_plays)
        assert saved == (40, 0.5, 3, 10, 2)
    tok = _login(client, "bob")  # another user's playlist
    r = client.post(f"/discover/{pid}/settings", data=good | {"csrf_token": tok, "size": "7"})
    assert r.status_code == 403
    with sf() as s:
        assert s.get_one(DiscoveryPlaylist, pid).size == 40


def test_build_now_enqueues_once_and_is_owner_only(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    pid = _seed(sf)
    tok = _login(client, "bob")
    assert client.post(f"/discover/{pid}/build", data={"csrf_token": tok}).status_code == 403
    tok = _login(client, "alice")
    for _ in range(2):
        assert client.post(f"/discover/{pid}/build", data={"csrf_token": tok}).status_code == 303
    with sf() as s:
        assert len(s.scalars(select(Job).where(Job.kind == JobKind.DISCOVERY_BUILD)).all()) == 1


def test_unlinked_user_sees_account_pointer(client: TestClient) -> None:
    _login(client, "bob")
    text = client.get("/discover").text
    assert 'href="/account"' in text and "Build now" not in text


def test_unauthenticated_redirects_to_login(client: TestClient) -> None:
    r = client.get("/discover")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.post(f"/discover/keep/{M['kept']}").status_code == 403  # no session, no CSRF


def _extra(sf: sessionmaker[Session], pid: int, mbid: str, **hold: object) -> None:
    with sf() as s, s.begin():
        uid = s.scalars(select(User.id).where(User.username == "alice")).one()
        pl = s.get_one(DiscoveryPlaylist, pid)
        entry = {"recording_mbid": mbid, "artist": "Art", "title": f"T-{mbid[:8]}"}
        pl.entries = [*pl.entries, entry]
        s.add(ProvisionalHold(user_id=uid, recording_mbid=mbid, playlist_id=pid, **hold))


def test_overdue_hold_is_labelled_and_missing_names_fall_back(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    pid = _seed(sf)
    old = "0000000a-0000-0000-0000-000000000000"
    with sf() as s, s.begin():
        pt = ProvisionalTrack(path="old.mp3", recording_mbid=old, batch_id="b", imported_at=NOW)
        s.add(pt)
        s.flush()
        pt_id = pt.id  # no title or artist, like an old row
    _extra(sf, pid, old, provisional_track_id=pt_id, expires_at=NOW - timedelta(days=5))
    _login(client, "alice")
    text = client.get("/discover").text
    soon = text.split("Expiring in the next 7 days")[1].split("<h2>Discovery")[0]
    assert "overdue" in soon and "None" not in soon and "Art - T-0000000a" in soon
    assert soon.count("overdue") == 1  # the one 3 days out is not overdue


def test_finished_request_without_a_file_is_not_found(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    pid = _seed(sf)
    failed, live, bare = (f"0000000{c}-0000-0000-0000-000000000000" for c in "bcd")
    with sf() as s, s.begin():
        uid = s.scalars(select(User.id).where(User.username == "alice")).one()
        reqs = {
            m: WorkRequest(user_id=uid, release_mbid="r", status=st, provisional=True)
            for m, st in ((failed, "failed"), (live, "searching"))
        }
        s.add_all(reqs.values())
        s.flush()
        ids = {m: r.id for m, r in reqs.items()}
    _extra(sf, pid, failed, request_id=ids[failed])
    _extra(sf, pid, live, request_id=ids[live])
    _extra(sf, pid, bare)  # a reset hold: no request yet
    _login(client, "alice")
    text = client.get("/discover").text.split("<h2>Discovery")[1]
    assert [_status(text, m[:8]) for m in (failed, live, bare)] == [
        "not found",
        "waiting for download",
        "waiting for download",
    ]


def test_last_error_is_escaped(client: TestClient, sf: sessionmaker[Session]) -> None:
    pid = _seed(sf)
    with sf() as s, s.begin():
        s.get_one(DiscoveryPlaylist, pid).last_error = "<script>alert(1)</script>"
    _login(client, "alice")
    text = client.get("/discover").text
    assert "<script>alert(1)" not in text and "&lt;script&gt;" in text


def test_posts_without_a_session_are_refused(client: TestClient, sf: sessionmaker[Session]) -> None:
    pid = _seed(sf)
    for path in (f"/discover/{pid}/settings", f"/discover/{pid}/build"):
        assert client.post(path, data={"size": "5"}).status_code in (303, 403)
    with sf() as s:
        assert s.get_one(DiscoveryPlaylist, pid).size == 50
        assert s.scalars(select(Job).where(Job.kind == JobKind.DISCOVERY_BUILD)).first() is None


def test_keeping_an_expired_hold_is_404(client: TestClient, sf: sessionmaker[Session]) -> None:
    _seed(sf)
    tok = _login(client, "alice")
    r = client.post(f"/discover/keep/{M['expired']}", data={"csrf_token": tok})
    assert r.status_code == 404
