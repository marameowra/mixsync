import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.requests import create_request
from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.core.jobs import JobKind, JobState
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.auth import User
from mixsync.db.models.library import Plan, Track
from mixsync.db.models.work import JOB_REQUEST_ID, Job
from mixsync.db.models.work import Request as AlbumRequest

HX = {"HX-Request": "true"}
MBIDS = [f"11111111-1111-1111-1111-11111111111{i}" for i in range(1, 5)]
TRIGGER = 'hx-trigger="every 5s"'


@pytest.fixture
def sf(settings: Settings) -> sessionmaker[Session]:
    upgrade()
    return make_session_factory(make_engine(settings))


@pytest.fixture
def client(sf: sessionmaker[Session]) -> Iterator[TestClient]:
    with sf() as s, s.begin():
        create_user(s, "alice", "pw")
        create_user(s, "bob", "pw")
    with TestClient(create_app(sf), follow_redirects=False) as c:
        assert c.post("/login", data={"username": "alice", "password": "pw"}).status_code == 303
        yield c


def add(sf: sessionmaker[Session], user: str, mbid: str, status: str) -> int:
    with sf() as s, s.begin():
        uid = s.query(User).filter_by(username=user).one().id
        req = create_request(s, datetime.now(UTC), uid, mbid)
        req.status = status
        s.flush()
        return req.id


def set_status(sf: sessionmaker[Session], req_id: int, status: str) -> None:
    with sf() as s, s.begin():
        s.get(AlbumRequest, req_id).status = status  # pyright: ignore[reportOptionalMemberAccess]


def poll_url(html: str) -> str:
    m = re.search(r'hx-get="([^"]+)"', html)
    assert m
    return m.group(1).replace("&amp;", "&")


def test_page_with_in_progress_request_has_polling_live_region(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    add(sf, "alice", MBIDS[0], "downloading")
    html = client.get("/requests").text
    assert '<div class="live-region"' in html and TRIGGER in html
    assert html.count('class="live-region"') == 1  # one region for the whole table


def test_fragment_returns_updated_status_and_stops_when_none_in_progress(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    rid = add(sf, "alice", MBIDS[0], "downloading")
    url = poll_url(client.get("/requests").text)
    busy = client.get(url, headers=HX)
    assert "<html" not in busy.text and TRIGGER in busy.text and "downloading" in busy.text
    set_status(sf, rid, "imported")
    done = client.get(url, headers=HX).text
    assert "<html" not in done and "imported" in done and "downloading" not in done
    assert "hx-get" not in done and TRIGGER not in done


def test_page_with_nothing_in_progress_does_not_poll(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    add(sf, "alice", MBIDS[0], "imported")
    assert "hx-get" not in client.get("/requests").text


def test_order_unchanged_after_status_change(client: TestClient, sf: sessionmaker[Session]) -> None:
    first = add(sf, "alice", MBIDS[0], "downloading")
    second = add(sf, "alice", MBIDS[1], "imported")
    url = poll_url(client.get("/requests").text)
    set_status(sf, first, "imported")
    html = client.get(url, headers=HX).text
    assert html.index(f"<td>{second}</td>") < html.index(f"<td>{first}</td>")
    third = add(sf, "alice", MBIDS[2], "downloading")  # a new request waits for a reload
    assert f"<td>{third}</td>" not in client.get(url, headers=HX).text


def test_fragment_never_exposes_another_users_request(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    mine = add(sf, "alice", MBIDS[0], "downloading")
    theirs = add(sf, "bob", MBIDS[1], "downloading")
    html = client.get(f"/requests?ids={mine}&ids={theirs}", headers=HX).text
    assert f"<td>{mine}</td>" in html and f"<td>{theirs}</td>" not in html


def add_track(sf: sessionmaker[Session], batch_id: str) -> int | None:
    with sf() as s, s.begin():
        t = Track(
            path=f"{batch_id}.flac",
            status="verified",
            batch_id=batch_id,
            imported_at=datetime.now(UTC),
        )
        s.add(t)
        s.flush()
        return t.request_id


def test_track_request_id_is_derived_from_its_batch_and_finds_the_music(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    with_music = add(sf, "alice", MBIDS[0], "imported")
    without = add(sf, "alice", MBIDS[1], "imported")
    assert add_track(sf, f"request-{with_music}-decision-3") == with_music
    assert add_track(sf, f"promote-{without}") is None
    html = client.get("/requests").text
    assert f"/requests/{with_music}/delete-with-music" in html
    assert f"/requests/{without}/delete-with-music" not in html


def test_delete_plan_link_shows_for_running_failed_and_confirmed_drafts(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    reqs = [add(sf, "alice", m, "imported") for m in MBIDS]
    plans: dict[str, int] = {}
    now = datetime.now(UTC)
    with sf() as s, s.begin():
        for rid, kind in zip(reqs, ("running", "failed", "draft", "confirmed"), strict=True):
            p = Plan(
                kind="delete",
                status="draft" if kind == "confirmed" else kind,
                release_group_mbid=MBIDS[0],
                canonical_release_mbid=MBIDS[0],
                ops=[{"op": "request", "request_id": rid}],
            )
            s.add(p)
            s.flush()
            plans[kind] = p.id
        # confirming a draft queues its job; a finished job does not count
        s.add(Job(kind=JobKind.MERGE, payload={"plan_id": plans["confirmed"]}, run_after=now))
        done = {"plan_id": plans["draft"]}
        s.add(Job(kind=JobKind.MERGE, payload=done, status=JobState.SUCCEEDED, run_after=now))
    html = client.get("/requests").text
    shown = {k for k, pid in plans.items() if f'href="/library/plans/{pid}"' in html}
    assert shown == {"running", "failed", "confirmed"}


def test_request_lookups_can_use_their_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On SQLite the job filter's text must equal ix_jobs_request_id's expression to use it.
    The schema comes from the migrations, so an edit to only one copy of it fails here."""
    url = f"sqlite:///{tmp_path / 'indexes.db'}"
    monkeypatch.setenv("MIXSYNC_DATABASE_URL", url)
    upgrade()
    engine = create_engine(url)
    queries = {
        "ix_jobs_request_id": select(Job.id).where(JOB_REQUEST_ID.in_([1, 2])),
        "ix_tracks_request_id": select(Track.id).where(Track.request_id == 1),
    }
    with engine.connect() as c:
        for index, q in queries.items():
            sql = str(q.compile(engine, compile_kwargs={"literal_binds": True}))
            plan = " ".join(str(r[-1]) for r in c.execute(text("EXPLAIN QUERY PLAN " + sql)))
            assert index in plan, plan
