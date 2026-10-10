"""The acquire pipeline end to end against fakes: the Phase 2 exit criteria 1 and 2, and the
rules around them (backoff, debounce, retries, review)."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mixsync.acquire.requests import create_request
from mixsync.core.jobs import JobKind, JobState
from mixsync.core.matching import TransferStatus
from mixsync.core.types import AcoustIdResult
from mixsync.db import decisions
from mixsync.db.models.library import Track
from mixsync.db.models.matching import MatchDecision
from mixsync.db.models.work import Request
from mixsync.library import tags

from .conftest import Env, login
from .fakes import ALBUM, ALBUM_B, OTHER

FIRST = "Alpha Band/Alpha Album (2020)/01-01 Song One.mp3"
SECOND = "Alpha Band/Alpha Album (2020)/01-02 Song Two.mp3"


def request(env: Env, mbid: str = str(ALBUM.release_id)) -> int:
    with env.sessions.begin() as s:
        return create_request(s, env.clock.now(), env.user_id, mbid).id


def status(env: Env, rid: int) -> str:
    with env.sessions() as s:
        req = s.get(Request, rid)
        assert req
        return req.status


def tried(env: Env, rid: int) -> list[list[str]]:
    with env.sessions() as s:
        req = s.get(Request, rid)
        assert req
        return req.tried


def library(env: Env) -> list[str]:
    lib = env.data_dir / "library"
    return sorted(str(p.relative_to(lib)) for p in lib.rglob("*") if p.is_file())


def decision_rows(env: Env, stage: int) -> list[MatchDecision]:
    with env.sessions() as s:
        return list(
            s.scalars(
                select(MatchDecision).where(MatchDecision.stage == stage).order_by(MatchDecision.id)
            )
        )


def veto_second_file(env: Env, folder: str = "Alpha Album") -> None:
    """AcoustID is sure (0.95) that the second file is a recording of another release."""
    env.acoustid.override[f"{folder}/02 Song Two.mp3"] = [
        AcoustIdResult("aid-x", 0.95, ("somebody-elses-recording",))
    ]


async def test_e1_requested_album_imports_with_mbids_and_rescans(env: Env) -> None:
    """Exit criterion 1: a requested album auto-imports with correct MBIDs, then a rescan."""
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    rid = request(env)
    await env.drain()

    assert status(env, rid) == "imported"
    assert library(env) == [FIRST, SECOND]
    for rel, track in zip((FIRST, SECOND), ALBUM.tracks, strict=True):
        t = tags.read_all(env.data_dir / "library" / rel)["tags"]
        assert t["TXXX:MusicBrainz Album Id"] == [ALBUM.release_id]
        assert t["TXXX:MusicBrainz Release Group Id"] == [ALBUM.release_group_id]
        assert t["TXXX:MIXSYNC_STATUS"] == ["verified"]
        assert t["UFID:http://musicbrainz.org"] == [track.recording_id]
    with env.sessions() as s:
        rows = {t.path: t for t in s.scalars(select(Track))}
    assert set(rows) == {FIRST, SECOND}
    assert rows[FIRST].recording_mbid == "Alpha-rec-1"
    assert rows[FIRST].release_mbid == ALBUM.release_id and rows[FIRST].status == "verified"
    assert env.target.rescans == 1
    assert [j.status for j in env.jobs(JobKind.RESCAN)] == [JobState.SUCCEEDED]
    # the stage 1 pick is not for review; the stage 2 decision was auto-accepted
    assert [d.status for d in decision_rows(env, 2)] == ["auto_accepted"]
    assert len(list((env.downloads / "Alpha Album").iterdir())) == 2  # sources stay


async def test_two_imports_close_together_share_one_rescan(env: Env) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    env.source.add("peer2", "Music\\Beta Band\\Beta Album", OTHER)
    ids = [request(env), request(env, str(OTHER.release_id))]
    await env.drain()

    assert [status(env, i) for i in ids] == ["imported", "imported"]
    assert len(library(env)) == 4
    assert env.target.rescans == 1


async def test_e2_bad_file_lands_in_review_then_accept(env: Env, web: TestClient) -> None:
    """Exit criterion 2: a file AcoustID disagrees with is held for review, not imported."""
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    veto_second_file(env)
    rid = request(env)
    await env.drain()

    assert status(env, rid) == "review"
    assert library(env) == []
    (d,) = [d for d in decision_rows(env, 2)]
    assert d.status == "pending_review" and d.vetoes
    tok = login(web, "alice")
    page = web.get(f"/review/{d.id}")
    assert page.status_code == 200 and "AcoustID (0.95) says a different recording" in page.text
    assert "somebody-elses-recording" in page.text
    assert f'href="/review/{d.id}"' in web.get("/review").text

    r = web.post(f"/review/{d.id}/accept", data={"csrf_token": tok})
    assert r.status_code == 303
    assert status(env, rid) == "importing"
    await env.drain()
    assert status(env, rid) == "imported" and library(env) == [FIRST, SECOND]
    assert env.target.rescans == 1


async def test_no_results_backs_off_then_caps(env: Env) -> None:
    rid = request(env)
    t0 = env.clock.now()
    for wait in [1, 6, 24, 168, 168]:  # hours: 1h, 6h, 24h, 1 week, then the cap
        job = None
        while job is None or job.kind is not JobKind.SEARCH:
            job = await env.step()
        assert status(env, rid) == "no_results"
        (nxt,) = [j for j in env.jobs(JobKind.SEARCH) if j.status is JobState.QUEUED]
        assert nxt.run_after.replace(tzinfo=env.clock.t.tzinfo) - env.clock.now() == timedelta(
            hours=wait
        )
    assert env.clock.now() - t0 == timedelta(hours=1 + 6 + 24 + 168)
    assert env.source.enqueued == []


async def test_search_is_debounced_for_ten_minutes(env: Env) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    env.source.state["peer1"] = TransferStatus.FAILED
    rid = request(env)
    t0 = env.clock.now()
    for kind in (JobKind.SEARCH, JobKind.DOWNLOAD, JobKind.DOWNLOAD, JobKind.SEARCH):
        job = await env.step()
        assert job and job.kind is kind
    assert status(env, rid) == "searching"  # the failed transfer re-queued a search ...
    seen = len(env.source.queries)
    (deferred,) = env.jobs(JobKind.SEARCH)[-1:]
    assert deferred.idempotency_key and deferred.idempotency_key.startswith("search:defer:")
    # ... which waited for next_search_at rather than hitting the peers again
    assert deferred.run_after.replace(tzinfo=t0.tzinfo) == t0 + timedelta(minutes=10)
    assert len(env.source.queries) == seen


async def test_failed_transfer_marks_tried_and_picks_next(env: Env) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    env.source.add("peer2", "Music\\Alpha Band\\Alpha Album [2]")
    env.source.state["peer1"] = TransferStatus.FAILED
    rid = request(env)
    await env.drain()

    assert env.source.enqueued == ["peer1", "peer2"]
    with env.sessions() as s:
        req = s.get(Request, rid)
        assert req and req.tried == [["peer1", "Music\\Alpha Band\\Alpha Album"]]
    assert status(env, rid) == "imported" and len(library(env)) == 2


@pytest.mark.parametrize("fault", ["wrong_size", "missing"])
async def test_bad_download_is_never_imported(env: Env, fault: str) -> None:
    if fault == "wrong_size":
        env.source.add("peer1", "Music\\Alpha Band\\Alpha Album", size_delta=1)
    else:
        env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
        env.source.write_nothing = True
    rid = request(env)
    await env.drain(until=lambda: bool(tried(env, rid)))

    assert env.jobs(JobKind.VERIFY) == [] and env.jobs(JobKind.IMPORT) == []
    assert library(env) == []
    with env.sessions() as s:
        req = s.get(Request, rid)
        assert req and req.tried and req.last_error


async def test_acoustid_outage_retries_verify_and_imports_nothing(env: Env) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    env.acoustid.fail = 1
    rid = request(env)
    await env.drain(until=lambda: bool(env.jobs(JobKind.VERIFY)) and env.acoustid.fail == 0)

    (verify,) = env.jobs(JobKind.VERIFY)
    assert verify.status is JobState.RETRY_WAIT and verify.max_attempts == 10
    assert status(env, rid) == "verifying" and library(env) == []
    assert decision_rows(env, 2) == []

    await env.drain()
    assert status(env, rid) == "imported" and len(library(env)) == 2


async def test_review_reject_tries_next_candidate_and_keeps_files(
    env: Env, web: TestClient
) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    env.source.add("peer2", "Music\\Alpha Band\\Alpha Album [2]")
    veto_second_file(env)
    rid = request(env)
    await env.drain()
    (d,) = decision_rows(env, 2)
    assert status(env, rid) == "review" and env.source.enqueued == ["peer1"]

    tok = login(web, "alice")
    assert web.post(f"/review/{d.id}/reject", data={"csrf_token": tok}).status_code == 303
    await env.drain()

    assert env.source.enqueued == ["peer1", "peer2"]
    assert status(env, rid) == "imported" and len(library(env)) == 2
    assert len(list((env.downloads / "Alpha Album").iterdir())) == 2  # rejected files are kept
    with env.sessions() as s:
        req = s.get(Request, rid)
        assert req and req.tried[0][0] == "peer1"


async def test_repick_verifies_against_the_chosen_release(env: Env, web: TestClient) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    veto_second_file(env)
    rid = request(env)
    await env.drain()
    (d,) = decision_rows(env, 2)

    tok = login(web, "alice")
    r = web.post(
        f"/review/{d.id}/repick", data={"csrf_token": tok, "release": str(ALBUM_B.release_id)}
    )
    assert r.status_code == 303 and status(env, rid) == "verifying"
    await env.drain()

    old, new = decision_rows(env, 2)
    assert (old.status, old.chosen_release_mbid) == ("repick", ALBUM_B.release_id)
    assert new.release_mbid == ALBUM_B.release_id and new.status == "pending_review"
    assert status(env, rid) == "review" and library(env) == []


async def test_stage_one_decisions_never_need_review(env: Env) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album", ext="flac")  # tiny "flac": veto
    request(env)
    await env.drain(until=lambda: bool(decision_rows(env, 1)))

    (d1,) = decision_rows(env, 1)
    assert d1.status == decisions.PENDING
    with env.sessions() as s:
        assert decisions.pending_for(s, None) == []


def test_new_request_form(env: Env, web: TestClient) -> None:
    tok = login(web, "alice")
    url = f"https://musicbrainz.org/release/{ALBUM.release_id}"
    assert web.post("/requests", data={"release": url}).status_code == 403  # no CSRF
    assert web.post("/requests", data={"csrf_token": tok, "release": url}).status_code == 303
    assert web.post("/requests", data={"csrf_token": tok, "release": "junk"}).status_code == 400
    with env.sessions() as s:
        (req,) = s.scalars(select(Request)).all()
    assert req.release_mbid == ALBUM.release_id and req.user_id == env.user_id
    (job,) = env.jobs(JobKind.SEARCH)
    assert job.payload == {"request_id": req.id}
    assert ALBUM.release_id in web.get("/requests").text

    tok = login(web, "nocap")
    r = web.post("/requests", data={"csrf_token": tok, "release": url})
    assert r.status_code == 403 and web.get("/requests").status_code == 403
    assert len(env.jobs(JobKind.SEARCH)) == 1
