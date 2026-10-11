"""The acquire pipeline end to end against fakes: the Phase 2 exit criteria 1 and 2, and the
rules around them (backoff, debounce, retries, review)."""

from dataclasses import replace
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from mixsync.acquire.requests import create_request
from mixsync.core.errors import PermanentError, TransientError
from mixsync.core.jobs import JobKind, JobState
from mixsync.core.matching import AlbumInfo, TrackInfo, TransferStatus
from mixsync.core.types import AcoustIdResult, ReleaseRef
from mixsync.db import canonical as canonical_db
from mixsync.db import decisions
from mixsync.db.auth import create_user
from mixsync.db.models.library import Track
from mixsync.db.models.matching import MatchDecision
from mixsync.db.models.work import Request
from mixsync.db.queue import enqueue_in
from mixsync.library import tags
from mixsync.library.fileops import WriteTags

from .conftest import Env, login
from .fakes import ALBUM, ALBUM_B, CATALOG, COVER, OTHER, RECORDINGS

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
    return sorted(str(p.relative_to(lib)) for p in lib.rglob("*.mp3"))


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
        assert t["TCON"] == list(ALBUM.genres)
    assert (env.data_dir / "library" / FIRST).with_name("cover.jpg").read_bytes() == COVER
    with env.sessions() as s:
        rows = {t.path: t for t in s.scalars(select(Track))}
    assert set(rows) == {FIRST, SECOND}
    assert rows[FIRST].recording_mbid == "Alpha-rec-1"
    assert rows[FIRST].release_mbid == ALBUM.release_id and rows[FIRST].status == "verified"
    assert env.target.rescans == 1
    assert [j.status for j in env.jobs(JobKind.RESCAN)] == [JobState.SUCCEEDED]
    # the stage 1 pick is not for review; the stage 2 decision was auto-accepted
    assert [d.status for d in decision_rows(env, 2)] == ["auto_accepted"]
    assert list(env.downloads.iterdir()) == []  # released: files and the album folder are gone
    assert env.source.cleared == ["peer1"]


async def test_partial_failure_releases_nothing(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    real = env.ops.import_file

    def flaky(src: Path, rel: str, write_tags: WriteTags, batch: str) -> str:
        if "Two" in src.name:
            raise OSError("disk full")
        return real(src, rel, write_tags, batch)

    monkeypatch.setattr(env.ops, "import_file", flaky)
    rid = request(env)
    await env.drain()

    assert status(env, rid) == "failed"
    assert len(list((env.downloads / "Alpha Album").iterdir())) == 2
    assert env.source.cleared == []


async def test_release_failure_does_not_fail_the_import(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")

    def boom(_srcs: list[Path], _batch: str) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(env.pipeline.importer, "release", boom)
    rid = request(env)
    await env.drain()
    assert status(env, rid) == "imported" and env.source.cleared == []


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


async def test_a_refusing_peer_tries_the_next_candidate(env: Env) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    env.source.add("peer2", "Music\\Alpha Band\\Alpha Album [2]")
    env.source.refuse.add("peer1")
    rid = request(env)
    await env.drain()

    assert env.source.enqueued == ["peer2"]
    with env.sessions() as s:
        req = s.get(Request, rid)
        assert req and req.tried == [["peer1", "Music\\Alpha Band\\Alpha Album"]]
    assert status(env, rid) == "imported"


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


def release_tags(env: Env, rel: str) -> dict[str, list[str]]:
    return tags.read_all(env.data_dir / "library" / rel)["tags"]


async def test_other_release_of_a_group_is_filed_under_the_canonical_one(env: Env) -> None:
    """ALBUM (2020) is the earliest release of the group, so it is canonical, even though the
    download is verified against ALBUM_B (2021)."""
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album", ALBUM_B)
    rid = request(env, str(ALBUM_B.release_id))
    await env.drain()

    assert status(env, rid) == "imported" and library(env) == [FIRST, SECOND]
    for rel in (FIRST, SECOND):
        assert release_tags(env, rel)["TXXX:MusicBrainz Album Id"] == [ALBUM.release_id]
    assert env.coverart.calls == [ALBUM.release_id]
    with env.sessions() as s:
        assert {t.release_mbid for t in s.scalars(select(Track))} == {ALBUM.release_id}


def add_deluxe(monkeypatch: pytest.MonkeyPatch) -> AlbumInfo:
    """A later release of ALBUM's group with one extra track."""
    bonus = TrackInfo("Song Three", length=1.0, index=3, medium_index=3, medium=1)
    bonus = replace(bonus, recording_id="Alpha-rec-3", artist_ids=("Alpha-artist",))
    deluxe = replace(
        ALBUM,
        release_id="44444444-4444-4444-4444-444444444444",
        year=2022,
        tracks=(*ALBUM.tracks, bonus),
    )
    monkeypatch.setitem(CATALOG, str(deluxe.release_id), deluxe)
    monkeypatch.setitem(RECORDINGS, "Song Three", "Alpha-rec-3")
    return deluxe


async def test_bonus_tracks_switch_the_canonical_release(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    deluxe = add_deluxe(monkeypatch)
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    request(env)
    await env.drain()
    old = library(env)
    assert old == [FIRST, SECOND]

    env.source.add("peer2", "Music\\Alpha Band\\Alpha Album Deluxe", deluxe)
    rid = request(env, str(deluxe.release_id))
    await env.drain()

    new = [f"Alpha Band/Alpha Album (2022)/01-0{i} {t}.mp3" for i, t in enumerate(
        ("Song One", "Song Two", "Song Three"), 1)]  # fmt: skip
    assert status(env, rid) == "imported" and library(env) == sorted(old + new)
    for rel in new:
        assert release_tags(env, rel)["TXXX:MusicBrainz Album Id"] == [deluxe.release_id]
    assert release_tags(env, FIRST)["TXXX:MusicBrainz Album Id"] == [ALBUM.release_id]
    with env.sessions() as s:
        assert canonical_db.get(s, str(ALBUM.release_group_id)) == deluxe.release_id


async def test_switch_that_lost_a_race_uses_the_stored_choice(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    deluxe = add_deluxe(monkeypatch)
    other = replace(deluxe, release_id="66666666-6666-6666-6666-666666666666", year=2021)
    monkeypatch.setitem(CATALOG, str(other.release_id), other)
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album", deluxe)
    real = canonical_db.switch

    def raced(s: Session, group: str, old: str, new: str) -> str:
        with env.sessions.begin() as other_session:  # another worker switches first
            real(other_session, group, old, str(other.release_id))
        return real(s, group, old, new)

    monkeypatch.setattr(canonical_db, "switch", raced)
    request(env, str(deluxe.release_id))
    await env.drain()

    assert library(env) == [
        f"Alpha Band/Alpha Album (2021)/01-0{i} {t}.mp3"
        for i, t in enumerate(("Song One", "Song Two", "Song Three"), 1)
    ]
    with env.sessions() as s:
        assert canonical_db.get(s, str(ALBUM.release_group_id)) == other.release_id


async def test_search_stops_when_every_recording_is_owned(env: Env) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    request(env)
    await env.drain()
    searched = list(env.source.queries)

    rid = request(env, str(ALBUM_B.release_id))
    await env.drain()
    assert status(env, rid) == "owned" and env.source.queries == searched
    assert env.source.enqueued == ["peer1"]


async def test_import_skips_recordings_the_group_already_has(env: Env) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album", ALBUM_B)
    with env.sessions.begin() as s:  # Song One is owned (from some other release of the group)
        s.add(
            Track(
                path="elsewhere.mp3",
                recording_mbid="Alpha-rec-1",
                release_group_mbid=ALBUM.release_group_id,
                status="verified",
                batch_id="old",
                imported_at=env.clock.now(),
            )
        )
    rid = request(env, str(ALBUM_B.release_id))
    await env.drain()

    assert status(env, rid) == "imported" and library(env) == [SECOND]
    assert [p.name for p in (env.downloads / "Alpha Album").iterdir()] == ["01 Song One.mp3"]


async def test_import_skips_owned_recordings_when_own_release_is_canonical(env: Env) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album", ALBUM)
    with env.sessions.begin() as s:  # Song One is owned (from some other release of the group)
        s.add(
            Track(
                path="elsewhere.mp3",
                recording_mbid="Alpha-rec-1",
                release_group_mbid=ALBUM.release_group_id,
                status="verified",
                batch_id="old",
                imported_at=env.clock.now(),
            )
        )
    rid = request(env, str(ALBUM.release_id))
    await env.drain()

    assert status(env, rid) == "imported" and library(env) == [SECOND]
    assert [p.name for p in (env.downloads / "Alpha Album").iterdir()] == ["01 Song One.mp3"]


async def test_canonical_choice_is_stored_and_reused(env: Env) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album", ALBUM_B)
    request(env, str(ALBUM_B.release_id))
    await env.drain()
    assert env.metadata.browsed == [ALBUM.release_group_id]

    # Another release of the group appears with an earlier date; the stored choice still wins.
    env.metadata.group[str(ALBUM_B.release_id)] = ReleaseRef(
        str(ALBUM_B.release_id), "x", "x", "1999", track_count=2
    )
    env.source.add("peer2", "Music\\Alpha Band\\Alpha Album 2", ALBUM_B)
    request(env, str(ALBUM_B.release_id))
    await env.drain()
    assert env.metadata.browsed == [ALBUM.release_group_id]


async def test_release_without_a_group_imports_as_is(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    ungrouped = replace(ALBUM_B, release_group_id=None)
    monkeypatch.setitem(CATALOG, str(ALBUM_B.release_id), ungrouped)
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album", ungrouped)
    request(env, str(ALBUM_B.release_id))
    await env.drain()

    assert env.metadata.browsed == []
    assert library(env) == [FIRST.replace("(2020)", "(2021)"), SECOND.replace("(2020)", "(2021)")]
    assert release_tags(env, library(env)[0])["TXXX:MusicBrainz Album Id"] == [ALBUM_B.release_id]


async def test_crash_after_importing_resumes_without_duplicates(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album", ALBUM_B)
    rid = request(env, str(ALBUM_B.release_id))
    real, calls = env.pipeline.importer.import_release, []

    def crashing(*a: Any) -> Any:
        calls.append(1)
        results = real(*a)
        if len(calls) == 1:  # the files are in, then the worker dies
            raise TransientError("killed")
        return results

    monkeypatch.setattr(env.pipeline.importer, "import_release", crashing)
    await env.drain()

    assert status(env, rid) == "imported" and library(env) == [FIRST, SECOND]
    assert env.metadata.browsed == [ALBUM.release_group_id]
    with env.sessions() as s:
        assert len(list(s.scalars(select(Track)))) == 2


async def test_canonical_without_shared_recordings_is_ignored(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different recording set (a different album sharing the group): file as the release."""
    foreign = replace(
        ALBUM,
        release_id="55555555-5555-5555-5555-555555555555",
        year=1999,
        tracks=tuple(replace(t, recording_id=f"x-{i}") for i, t in enumerate(ALBUM.tracks)),
    )
    monkeypatch.setitem(CATALOG, str(foreign.release_id), foreign)
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album", ALBUM_B)
    with env.sessions.begin() as s:
        canonical_db.store(s, str(ALBUM.release_group_id), str(foreign.release_id))
    request(env, str(ALBUM_B.release_id))
    await env.drain()

    rels = [r.replace("(2020)", "(2021)") for r in (FIRST, SECOND)]
    assert library(env) == rels
    assert release_tags(env, rels[0])["TXXX:MusicBrainz Album Id"] == [ALBUM_B.release_id]


async def test_empty_browse_imports_under_the_own_release(env: Env) -> None:
    async def nothing(_group: str) -> list[ReleaseRef]:
        return []

    env.metadata.browse_group_releases = nothing  # type: ignore[method-assign]
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album", ALBUM_B)
    request(env, str(ALBUM_B.release_id))
    await env.drain()

    assert library(env) == [FIRST.replace("(2020)", "(2021)"), SECOND.replace("(2020)", "(2021)")]
    with env.sessions() as s:
        assert canonical_db.get(s, str(ALBUM.release_group_id)) is None


def test_store_keeps_an_existing_choice(env: Env) -> None:
    with env.sessions.begin() as s:
        assert canonical_db.store(s, "g", "first") == "first"
    with env.sessions.begin() as s:
        assert canonical_db.store(s, "g", "second") == "first"


async def test_switch_survives_a_crash_and_still_imports_every_file(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    deluxe = add_deluxe(monkeypatch)
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    request(env)
    await env.drain()
    env.source.add("peer2", "Music\\Alpha Band\\Alpha Album Deluxe", deluxe)
    rid = request(env, str(deluxe.release_id))
    real, calls = env.pipeline.importer.import_release, []

    def crashing(*a: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:  # the switch is committed, then the worker dies
            raise TransientError("killed")
        return real(*a)

    monkeypatch.setattr(env.pipeline.importer, "import_release", crashing)
    await env.drain()

    new = [f"Alpha Band/Alpha Album (2022)/01-0{i} {t}.mp3" for i, t in enumerate(
        ("Song One", "Song Two", "Song Three"), 1)]  # fmt: skip
    assert status(env, rid) == "imported" and library(env) == sorted([FIRST, SECOND, *new])
    assert env.metadata.browsed == [ALBUM.release_group_id]


async def test_import_of_only_owned_recordings_finishes_as_owned(env: Env) -> None:
    """Both requests search before either has imported, so only the import can see the dupes."""
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    first, second = request(env), request(env)
    await env.drain()
    assert sorted([status(env, first), status(env, second)]) == ["imported", "owned"]
    assert len(env.jobs(JobKind.IMPORT)) == 2 and library(env) == [FIRST, SECOND]


async def test_mark_unverified_imports_the_edited_tags_without_ids(
    env: Env, web: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    veto_second_file(env)
    rid = request(env)
    await env.drain()
    (d,) = decision_rows(env, 2)
    tok = login(web, "alice")
    page = web.get(f"/review/{d.id}").text
    assert 'value="Alpha Album"' in page and 'value="Song Two"' in page  # prefilled from tags
    form = {
        "albumartist": "Edited Band",
        "album": "Edited Album",
        "year": "1999",
        "title": ["Edited One", "Edited Two"],
        "artist": ["", "Guest"],
        "disc": ["1", "1"],
        "track": ["1", "2"],
    }
    # a crash after the files are in: the retry must not import them twice
    real, calls = env.pipeline.importer.import_release, []

    def crashing(*a: Any) -> Any:
        calls.append(1)
        results = real(*a)
        if len(calls) == 1:
            raise TransientError("killed")
        return results

    monkeypatch.setattr(env.pipeline.importer, "import_release", crashing)
    assert web.post(f"/review/{d.id}/unverified", data=form).status_code == 403  # no CSRF token
    r = web.post(f"/review/{d.id}/unverified", data=form | {"csrf_token": tok})
    assert r.status_code == 303
    assert status(env, rid) == "importing"
    await env.drain()

    one = "Edited Band/Edited Album (1999)/01-01 Edited One.mp3"
    two = "Edited Band/Edited Album (1999)/01-02 Edited Two.mp3"
    assert status(env, rid) == "imported" and library(env) == [one, two]
    for rel, artist in ((one, "Edited Band"), (two, "Guest")):
        t = release_tags(env, rel)
        assert t["TXXX:MIXSYNC_STATUS"] == ["unverified"] and t["TPE1"] == [artist]
        assert "TCON" not in t
        assert not any(k.startswith(("UFID", "TXXX:MusicBrainz", "TXXX:Acoustid")) for k in t)
    with env.sessions() as s:
        rows = list(s.scalars(select(Track)))
    assert sorted(r.path for r in rows) == [one, two]
    assert {r.status for r in rows} == {"unverified"}
    assert not any(r.recording_mbid or r.release_mbid or r.acoustid_id for r in rows)
    assert list(env.downloads.iterdir()) == [] and env.source.cleared == ["peer1"]
    assert env.target.rescans == 1 and len(calls) == 2
    assert not (env.data_dir / "library" / one).with_name("cover.jpg").exists()
    (check,) = env.jobs(JobKind.RECHECK)  # the first background check, in a week or so
    assert check.payload == {"album": ["Edited Band", "Edited Album", "1999"], "user_id": None,
                             "attempt": 0}  # fmt: skip
    assert check.run_after.replace(tzinfo=UTC) - check.created_at.replace(tzinfo=UTC) > timedelta(
        days=3
    )

    with env.sessions.begin() as s:
        create_user(s, "root", "pw", admin=True)
    login(web, "root")  # the library view (library.edit) and the home count
    assert "2 unverified tracks" in web.get("/").text
    assert web.get("/library/unverified").status_code == 200
    login(web, "alice")
    assert web.get("/library/unverified").status_code == 403
    login(web, "root")
    html = web.get("/library/unverified").text
    assert one in html and "Edited One" in html and "Edited Album" in html


async def _review_form(env: Env, web: TestClient, **edits: Any) -> tuple[int, int, dict[str, Any]]:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    veto_second_file(env)
    rid = request(env)
    await env.drain()
    (d,) = decision_rows(env, 2)
    form: dict[str, Any] = {
        "csrf_token": login(web, "alice"),
        "albumartist": "Band",
        "album": "Album",
        "year": "",
        "title": ["One", "Two"],
        "artist": ["", ""],
        "disc": ["1", "1"],
        "track": ["1", "2"],
    }
    return rid, d.id, form | edits


async def test_unverified_paths_stay_inside_the_library(env: Env, web: TestClient) -> None:
    rid, did, form = await _review_form(
        env, web, albumartist="../../etc", album="..", title=["A/../x", "B"]
    )
    assert web.post(f"/review/{did}/unverified", data=form).status_code == 303
    await env.drain()
    assert status(env, rid) == "imported"
    lib = (env.data_dir / "library").resolve()
    found = library(env)
    assert len(found) == 2
    for rel in found:
        assert ".." not in Path(rel).parts and (lib / rel).resolve().is_relative_to(lib)
    assert not (env.data_dir.parent / "etc").exists() and not (env.data_dir / "etc").exists()


async def test_unverified_partial_failure_releases_nothing(
    env: Env, web: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid, did, form = await _review_form(env, web)
    real = env.ops.import_file

    def flaky(src: Path, rel: str, write_tags: WriteTags, batch: str) -> str:
        if "Two" in src.name:
            raise OSError("disk full")
        return real(src, rel, write_tags, batch)

    monkeypatch.setattr(env.ops, "import_file", flaky)
    assert web.post(f"/review/{did}/unverified", data=form).status_code == 303
    await env.drain()
    assert status(env, rid) == "failed"
    assert len(list((env.downloads / "Alpha Album").iterdir())) == 2
    assert env.source.cleared == []


async def test_a_rerun_verify_records_nothing_more(env: Env) -> None:
    """A job re-run after a lock hit complete() must not add a second review item or import."""
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    veto_second_file(env)
    request(env)
    await env.drain()
    (job,) = env.jobs(JobKind.VERIFY)
    before = len(decision_rows(env, 2))
    await env.handlers[JobKind.VERIFY](env.queue.get(job.id))
    assert len(decision_rows(env, 2)) == before == 1


async def test_a_verify_superseded_by_a_repick_records_nothing(env: Env) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    veto_second_file(env)
    rid = request(env)
    await env.drain()
    (old,) = env.jobs(JobKind.VERIFY)
    with env.sessions.begin() as s:  # what a repick leaves behind
        req = s.get(Request, rid)
        assert req
        req.status = "verifying"
        enqueue_in(s, env.clock.now(), JobKind.VERIFY, old.payload)
    before = len(decision_rows(env, 2))
    await env.handlers[JobKind.VERIFY](env.queue.get(old.id))  # the stale re-run
    assert len(decision_rows(env, 2)) == before


async def test_a_repick_of_a_nonexistent_release_reopens_the_review(
    env: Env, web: TestClient
) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    veto_second_file(env)
    rid = request(env)
    await env.drain()
    (d,) = decision_rows(env, 2)
    ghost = "44444444-4444-4444-4444-444444444444"
    tok = login(web, "alice")
    r = web.post(f"/review/{d.id}/repick", data={"csrf_token": tok, "release": ghost})
    assert r.status_code == 303

    async def missing(mbid: str) -> Any:
        raise PermanentError("returned 404")

    env.pipeline.metadata.get_release = missing  # type: ignore[method-assign]
    real = env.acoustid.lookup
    lookups: list[Any] = []

    async def counted(fp: Any) -> Any:
        lookups.append(fp)
        return await real(fp)

    env.acoustid.lookup = counted  # type: ignore[method-assign]
    await env.drain()
    assert lookups == []  # the missing release is found before any AcoustID call
    (d,) = decision_rows(env, 2)
    assert (d.status, d.final_action, d.chosen_release_mbid) == ("pending_review", None, None)
    with env.sessions() as s:
        req = s.get(Request, rid)
        assert req and (req.status, req.last_error) == (
            "review",
            f"release {ghost} not found on MusicBrainz",
        )
