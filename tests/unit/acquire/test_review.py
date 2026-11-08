"""The Review operations, called directly: each settles the decision and moves its request."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire import review, verify
from mixsync.acquire.evidence import Evidence
from mixsync.acquire.requests import candidate_dict, create_request
from mixsync.core.config import Settings
from mixsync.core.jobs import JobKind
from mixsync.core.matching import AlbumInfo, Band, Candidate, CandidateFile, MatchResult, Penalty
from mixsync.db import decisions
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.matching import MatchDecision
from mixsync.db.models.work import Job, Request

MBID = "22222222-2222-2222-2222-222222222222"
OTHER = "33333333-3333-3333-3333-333333333333"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def sf(settings: Settings) -> sessionmaker[Session]:
    upgrade()
    return make_session_factory(make_engine(settings))


@pytest.fixture
def s(sf: sessionmaker[Session]) -> Iterator[Session]:
    with sf() as s, s.begin():
        yield s


@pytest.fixture
def uid(sf: sessionmaker[Session]) -> int:
    with sf() as s, s.begin():
        return create_user(s, "alice", "pw").id


def reviewed(
    s: Session, uid: int, path: str, *, release: str = MBID, source: str = "manual"
) -> tuple[MatchDecision, Request]:
    """A pending stage 2 decision of a request waiting in review."""
    req = create_request(s, NOW, uid, MBID, source=source)
    req.status = "review"
    cand = Candidate("peer1", "Music\\Album", (CandidateFile("a.mp3", 0, "mp3"),))
    ev = Evidence.verified(
        req.id,
        {},
        cand,
        [Path(path)],
        AlbumInfo("Artist", "Album", ()),
        unassigned=[],
        partial="",
        acoustid=[],
        duration_deltas=[],
        assignment=[],
    )
    res = MatchResult(0.2, Band.REVIEW, (Penalty("title", 0.5, 2.0, "title differs"),), (), 1)
    d = decisions.record(s, res, evidence=ev.to_json(), user_id=uid, stage=2, release_mbid=release)
    return d, req


def jobs(s: Session, kind: JobKind, req: Request) -> list[Job]:
    q = select(Job).where(Job.kind == kind, Job.payload["request_id"].as_integer() == req.id)
    return list(s.scalars(q))


def test_accept_queues_the_import(s: Session, uid: int) -> None:
    d, req = reviewed(s, uid, "/x")
    review.accept(s, NOW, d.id, uid)
    assert (d.status, d.final_action, d.acted_by) == ("accepted", "accept", uid)
    assert d.acted_at and req.status == "importing"
    (job,) = jobs(s, JobKind.IMPORT, req)
    assert job.payload["decision_id"] == d.id


def test_accept_refuses_without_a_release_and_leaves_the_decision(s: Session, uid: int) -> None:
    d, req = reviewed(s, uid, "/x", release="")
    with pytest.raises(review.Refused, match="no release"):
        review.accept(s, NOW, d.id, uid)
    assert d.status == decisions.PENDING and req.status == "review"


def refiles(s: Session) -> list[int]:
    return [
        j.payload["decision_id"] for j in s.scalars(select(Job).where(Job.kind == JobKind.REFILE))
    ]


def test_reject_drops_the_candidate_and_searches_again(s: Session, uid: int) -> None:
    d, req = reviewed(s, uid, "/x")
    review.reject(s, NOW, d.id, uid)
    assert (d.status, d.acted_by) == ("rejected", uid)
    assert req.tried == [["peer1", "Music\\Album"]] and req.status != "review"
    assert len(jobs(s, JobKind.SEARCH, req)) == 2
    assert refiles(s) == [d.id]


def test_reject_of_a_from_files_request_marks_it_rejected(s: Session, uid: int) -> None:
    d, req = reviewed(s, uid, "/x", source="drop")
    review.reject(s, NOW, d.id, uid)
    assert (req.status, req.last_error) == ("skipped", "rejected in review")
    assert len(jobs(s, JobKind.SEARCH, req)) == 1  # no new search
    assert refiles(s) == [d.id]  # its files leave quarantine/review too


def test_repick_verifies_against_the_chosen_release(s: Session, uid: int) -> None:
    d, req = reviewed(s, uid, "/x")
    review.repick(s, NOW, d.id, uid, OTHER)
    assert (d.status, d.chosen_release_mbid, d.acted_by) == ("repick", OTHER, uid)
    assert verify.running(s, req.id)
    (job,) = jobs(s, JobKind.VERIFY, req)
    assert job.payload["release_mbid"] == OTHER


def test_unverified_stores_the_edits_and_queues_the_import(s: Session, uid: int) -> None:
    d, req = reviewed(s, uid, "/x")
    edits = {"albumartist": "Band", "album": "Album", "year": None, "tracks": []}
    review.unverified(s, NOW, d.id, uid, edits)
    assert (d.status, d.acted_by) == ("unverified", uid)
    assert d.evidence["unverified"] == edits and req.status == "importing"
    assert len(jobs(s, JobKind.IMPORT, req)) == 1


@pytest.mark.parametrize("files", [[], [{"name": "a.mp3"}]])
def test_unverified_refuses_unknown_files(s: Session, uid: int, files: list[object]) -> None:
    d, req = reviewed(s, uid, "/x")
    d.evidence = {**d.evidence, "source": {**d.evidence["source"], "files": files}}
    with pytest.raises(review.Refused, match="not known"):
        review.unverified(s, NOW, d.id, uid, {})
    assert d.status == decisions.PENDING and req.status == "review"


def test_operations_refuse_a_stage_one_decision(s: Session, uid: int) -> None:
    d, _ = reviewed(s, uid, "/x")
    d.stage = 1
    with pytest.raises(review.Refused):
        review.reject(s, NOW, d.id, uid)
    with pytest.raises(review.Refused):
        review.unverified(s, NOW, d.id, uid, {})


def test_two_actions_on_one_decision_only_one_wins(sf: sessionmaker[Session], uid: int) -> None:
    with sf() as s, s.begin():
        did = reviewed(s, uid, "/x")[0].id
    with sf() as first, sf() as second:
        second.get_one(MatchDecision, did)  # both read it pending
        with first.begin():
            review.accept(first, NOW, did, uid)
        with pytest.raises(review.Refused):
            review.reject(second, NOW, did, uid)
    with sf() as s:
        d = s.get_one(MatchDecision, did)
        req = s.get_one(Request, Evidence.parse(d.evidence).request_id)
        assert (d.status, req.status, req.tried) == ("accepted", "importing", [])


@pytest.fixture
def audio(tmp_path: Path) -> Path:
    p = tmp_path / "a.mp3"
    p.write_bytes(b"x")
    return p


def test_recheck_queued(s: Session, uid: int, audio: Path) -> None:
    d, req = reviewed(s, uid, str(audio))
    assert review.recheck(s, NOW, d.id, uid) == "queued"
    assert (d.status, d.final_action, d.acted_by) == ("rechecked", "recheck", uid)
    assert req.status == "verifying" and verify.running(s, req.id)


def test_recheck_missing(s: Session, uid: int, audio: Path) -> None:
    gone, _ = reviewed(s, uid, str(audio.parent / "gone.mp3"))
    loose, _ = reviewed(s, uid, str(audio))
    loose.evidence = {k: v for k, v in loose.evidence.items() if k != "request_id"}
    assert review.recheck(s, NOW, gone.id, uid) == "missing"
    assert review.recheck(s, NOW, loose.id, uid) == "missing"
    assert gone.status == loose.status == decisions.PENDING


def test_recheck_stale(s: Session, uid: int, audio: Path) -> None:
    d, req = reviewed(s, uid, str(audio))
    req.status = "importing"
    assert review.recheck(s, NOW, d.id, uid) == "stale"
    assert d.status == decisions.PENDING


def test_recheck_busy_when_verify_runs_or_the_decision_was_taken(
    s: Session, uid: int, audio: Path
) -> None:
    d, req = reviewed(s, uid, str(audio))
    verify.start(s, NOW, req, None, {}, [])
    req.status = "review"
    assert review.recheck(s, NOW, d.id, uid) == "busy"
    assert d.status == decisions.PENDING
    taken, _ = reviewed(s, uid, str(audio))
    decisions.act(s, taken.id, uid, "reject")
    assert review.recheck(s, NOW, taken.id, uid) == "busy"


def test_cancel_closes_pending_reviews_as_cancelled(s: Session, uid: int) -> None:
    d, req = reviewed(s, uid, "/x")
    other, _ = reviewed(s, uid, "/y")
    stage1, _ = reviewed(s, uid, "/z")
    stage1.stage = 1
    stage1.evidence = {**stage1.evidence, "request_id": req.id}
    review.cancel(s, req.id, uid)
    assert (d.status, d.final_action, d.acted_by) == ("cancelled", "cancel", uid)
    assert d.acted_at
    assert other.status == stage1.status == decisions.PENDING


def test_cancel_refuses_when_a_review_acts_first(
    s: Session, uid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, req = reviewed(s, uid, "/x")

    def raced(*_a: object, **_k: object) -> None:
        raise decisions.DecisionStateError("already acted on")

    monkeypatch.setattr(decisions, "act", raced)
    with pytest.raises(review.Refused, match="a review is acting"):
        review.cancel(s, req.id, uid)


def splittable(s: Session, uid: int, tmp: Path, *, provisional: bool = False) -> MatchDecision:
    """A pending decision of three files: a.mp3 paired, b.mp3 and c.mp3 not on the release."""
    wanted = ["rec-a"] if provisional else []
    req = create_request(s, NOW, uid, MBID, provisional=provisional, recordings=wanted)
    req.status = "review"
    names = ("a.mp3", "b.mp3", "c.mp3")
    paths = [tmp / n for n in names]
    for p in paths:
        p.write_bytes(b"x")
    cand = Candidate("peer1", "Music\\Album", tuple(CandidateFile(n, 1, "mp3") for n in names))
    ev = Evidence.verified(
        req.id,
        candidate_dict(cand),
        cand,
        paths,
        AlbumInfo("Artist", "Album", ()),
        unassigned=[str(paths[1]), str(paths[2])],
        partial="",
        acoustid=[],
        duration_deltas=[0.0],
        assignment=[{"local_path": str(paths[0]), "track": 0, "acoustid_id": None}],
    )
    res = MatchResult(0.3, Band.REVIEW, (), (), 1)
    return decisions.record(s, res, evidence=ev.to_json(), user_id=uid, stage=2, release_mbid=MBID)


def test_split_makes_a_from_files_request_and_leaves_the_decision_open(
    s: Session, uid: int, tmp_path: Path
) -> None:
    d = splittable(s, uid, tmp_path)
    b = str(tmp_path / "b.mp3")
    new = review.split(s, NOW, d.id, [b], OTHER)
    assert (new.source, new.user_id, new.release_mbid) == ("split", uid, OTHER)
    assert new.from_files and new.origin and new.origin["paths"] == [b]
    assert [f["path"] for f in new.origin["candidate"]["files"]] == ["b.mp3"]
    (job,) = jobs(s, JobKind.VERIFY, new)
    assert (job.payload["release_mbid"], job.payload["paths"]) == (OTHER, [b])
    assert jobs(s, JobKind.SEARCH, new) == []
    assert (d.status, d.acted_by, d.distance, d.band) == (decisions.PENDING, None, 0.3, "review")
    ev = Evidence.parse(d.evidence)
    assert ev.unassigned == (str(tmp_path / "c.mp3"),)
    assert ev.split_to == {b: new.id}
    assert b not in ev.paths and len(ev.listed) == 3  # later actions leave b alone
    assert [f["path"] for f in ev.candidate["files"]] == ["a.mp3", "c.mp3"]


def test_two_splits_of_one_decision_make_two_requests(s: Session, uid: int, tmp_path: Path) -> None:
    d = splittable(s, uid, tmp_path)
    one = review.split(s, NOW, d.id, [str(tmp_path / "b.mp3")], OTHER)
    two = review.split(s, NOW, d.id, [str(tmp_path / "c.mp3")], MBID)
    assert one.id != two.id and d.status == decisions.PENDING
    ev = Evidence.parse(d.evidence)
    assert ev.unassigned == () and set(ev.split_to.values()) == {one.id, two.id}


def test_a_repick_after_a_split_verifies_only_the_rest(
    s: Session, uid: int, tmp_path: Path
) -> None:
    d = splittable(s, uid, tmp_path)
    review.split(s, NOW, d.id, [str(tmp_path / "b.mp3")], OTHER)
    parent = s.get_one(Request, Evidence.parse(d.evidence).request_id)
    review.repick(s, NOW, d.id, uid, OTHER)
    (job,) = jobs(s, JobKind.VERIFY, parent)
    assert job.payload["paths"] == [str(tmp_path / "a.mp3"), str(tmp_path / "c.mp3")]
    assert [f["path"] for f in job.payload["candidate"]["files"]] == ["a.mp3", "c.mp3"]


def refused(s: Session, d: MatchDecision, paths: list[str], mbid: str = OTHER) -> None:
    def state() -> tuple[object, ...]:
        n = s.scalar(select(func.count()).select_from(Request))
        verifies = s.scalar(select(func.count()).select_from(Job).where(Job.kind == JobKind.VERIFY))
        return dict(d.evidence), d.status, n, verifies

    before = state()
    with pytest.raises(review.Refused):
        review.split(s, NOW, d.id, paths, mbid)
    assert state() == before


def test_split_refusals_change_nothing(s: Session, uid: int, tmp_path: Path) -> None:
    d = splittable(s, uid, tmp_path)
    b = str(tmp_path / "b.mp3")
    refused(s, d, [])  # no files
    refused(s, d, [str(tmp_path / "a.mp3")])  # paired: repick covers a wrong release
    refused(s, d, [str(tmp_path / "elsewhere.mp3")])
    refused(s, d, [b], "")  # no release
    refused(s, d, [b], "not-an-mbid")
    (tmp_path / "c.mp3").unlink()
    refused(s, d, [str(tmp_path / "c.mp3")])  # gone from disk
    d.stage = 1
    refused(s, d, [b])
    d.stage = 2
    decisions.act(s, d.id, uid, "reject")
    refused(s, d, [b])  # no longer open for review


def test_split_refuses_a_discovery_review(s: Session, uid: int, tmp_path: Path) -> None:
    d = splittable(s, uid, tmp_path, provisional=True)
    refused(s, d, [str(tmp_path / "b.mp3")])
