"""Audio analysis (#447, ADR 0025): features on generated audio, the ANALYZE job's place in the
queue, backfill, re-analysis on a feature version change, failures, the off switch, removal,
and the neighbour query."""

import asyncio
import importlib.util
import sys
import wave
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import SystemClock
from mixsync.core.config import Settings
from mixsync.core.jobs import LIVE, JobKind
from mixsync.db.models.library import Track, TrackAnalysis
from mixsync.db.models.work import Job
from mixsync.db.queue import JobQueue, enqueue_in
from mixsync.library import analysis, similarity
from mixsync.library.analysis import ANALYZE_PRIORITY, AudioAnalysis, Features, analyze_file
from mixsync.library.fileops import FileOps
from mixsync.library.importer import Importer

from .conftest import MakeSrc
from .test_importer import ALBUM, T1

SR = 44100
BLISS = importlib.util.find_spec("bliss_audio") is not None  # the image builds it from source


def _wav(path: Path, y: np.ndarray) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def clicks(path: Path, bpm: float, seconds: float = 30) -> Path:
    """A 1 kHz click on every beat."""
    y = np.zeros(int(seconds * SR))
    t = np.arange(int(0.01 * SR)) / SR
    click = np.sin(2 * np.pi * 1000 * t) * np.exp(-t * 300)
    for beat in np.arange(0, seconds, 60 / bpm):
        i = int(beat * SR)
        y[i : i + len(click)] += click[: len(y) - i]
    return _wav(path, 0.8 * y)


def triad(path: Path, *hz: float, seconds: float = 10) -> Path:
    t = np.arange(int(seconds * SR)) / SR
    return _wav(path, 0.5 * np.mean([np.sin(2 * np.pi * f * t) for f in hz], axis=0))


@pytest.mark.parametrize("bpm", [90, 120, 140])
def test_bpm_of_a_click_track_is_within_2(tmp_path: Path, bpm: int) -> None:
    assert abs(analyze_file(clicks(tmp_path / "c.wav", bpm)).bpm - bpm) <= 2


@pytest.mark.parametrize(
    ("hz", "key"),
    [
        ((261.63, 329.63, 392.0), ("C", "major")),
        ((220.0, 261.63, 329.63), ("A", "minor")),
        ((369.99, 466.16, 554.37), ("F#", "major")),
    ],
)
def test_the_key_of_a_pure_triad(
    tmp_path: Path, hz: tuple[float, ...], key: tuple[str, str]
) -> None:
    f = analyze_file(triad(tmp_path / "t.wav", *hz))
    assert (f.key_tonic, f.key_mode) == key


def test_features_are_stable_across_runs(tmp_path: Path) -> None:
    path = triad(tmp_path / "t.wav", 261.63, 329.63, 392.0)
    first, second = analyze_file(path), analyze_file(path)
    assert first == second
    assert -10 > first.energy > -30  # a half-scale mix of sines: about -15 dBFS
    # the vector comes from bliss, which the image has and macOS development lacks
    assert (first.vector is not None) == BLISS
    if first.vector is not None:
        assert len(first.vector) >= 20 and all(np.isfinite(first.vector))


def test_silence_has_no_key_and_garbage_raises(tmp_path: Path) -> None:
    f = analyze_file(_wav(tmp_path / "s.wav", np.zeros(SR * 5)))
    assert (f.key_tonic, f.key_mode) == (None, None) and f.energy == -200
    (tmp_path / "x.flac").write_bytes(b"not audio")
    with pytest.raises(Exception):  # noqa: B017  # ffmpeg's error, recorded by the job
        analyze_file(tmp_path / "x.flac")


# --- the job -------------------------------------------------------------------------------


def fake(path: Path) -> Features:
    n = float(path.name.removesuffix(".flac"))
    return Features(120.0, "C", "major", -12.0, [n, 0.0])


def add_tracks(sessions: sessionmaker[Session], *names: str) -> list[int]:
    with sessions.begin() as s:
        rows = [
            Track(path=f"{n}.flac", status="verified", batch_id="b", imported_at=datetime.now(UTC))
            for n in names
        ]
        s.add_all(rows)
        s.flush()
        return [r.id for r in rows]


def jobs(sessions: sessionmaker[Session]) -> list[Job]:
    with sessions() as s:
        return list(s.scalars(select(Job).where(Job.kind == JobKind.ANALYZE).order_by(Job.id)))


def rows(sessions: sessionmaker[Session]) -> dict[int, TrackAnalysis]:
    with sessions() as s:
        return {r.track_id: r for r in s.scalars(select(TrackAnalysis))}


def engine_of(sessions: sessionmaker[Session]) -> Engine:
    bind = sessions.kw["bind"]
    assert isinstance(bind, Engine)
    return bind


def drain(sessions: sessionmaker[Session], a: AudioAnalysis) -> int:
    """Run ANALYZE jobs through the real queue until none is left; returns how many ran."""
    queue, ran = JobQueue(engine_of(sessions), SystemClock(), lambda *_: None), 0
    while job := queue.claim("w"):
        queue.start(job.id, "w")
        a.analyze_one(job.payload.get("track_id"))
        queue.complete(job.id, "w")
        ran += 1
    return ran


def make(
    sessions: sessionmaker[Session],
    ops: FileOps,
    enabled: bool = True,
    analyze: analysis.Analyze = fake,
    version: int = analysis.FEATURE_VERSION,
) -> AudioAnalysis:
    return AudioAnalysis(sessions, SystemClock(), ops.library, enabled, analyze, version)


def test_an_import_queues_analysis_last_behind_imports_and_searches(
    sessions: sessionmaker[Session], ops: FileOps, make_src: MakeSrc, settings: Settings
) -> None:
    importer = Importer(ops, sessions, SystemClock(), settings.path_template, analyze=True)
    [r] = importer.import_release([(make_src("flac", "a"), T1, None)], ALBUM, "verified", "b")
    [job] = jobs(sessions)
    with sessions() as s:
        track = s.scalars(select(Track)).one()
        assert job.payload == {"track_id": track.id} and r.rel == track.path
        assert job.priority == ANALYZE_PRIORITY
    with sessions.begin() as s:  # queued after it, claimed before it
        now = datetime.now(UTC)
        enqueue_in(s, now, JobKind.IMPORT, {"request_id": 1}, priority=1)  # a migration import
        enqueue_in(s, now, JobKind.SEARCH, {"request_id": 2}, priority=1)  # a discovery search
    queue = JobQueue(engine_of(sessions), SystemClock(), lambda *_: None)
    claimed = [queue.claim("w") for _ in range(3)]
    assert [c.kind for c in claimed if c] == [JobKind.IMPORT, JobKind.SEARCH, JobKind.ANALYZE]


def test_the_backfill_runs_one_job_at_a_time_until_every_track_is_analyzed(
    sessions: sessionmaker[Session], ops: FileOps
) -> None:
    ids = add_tracks(sessions, "1", "2", "3")
    a = make(sessions, ops)
    a.start_chains()
    a.start_chains()  # a live job already carries the chain
    assert len(jobs(sessions)) == 1
    assert drain(sessions, a) == 3
    got = rows(sessions)
    assert sorted(got) == ids and all(r.failure is None and r.bpm == 120.0 for r in got.values())
    assert got[ids[1]].vector == [2.0, 0.0] and got[ids[1]].analyzer.endswith(
        f"librosa {analysis.version('librosa')}"
    )
    a.start_chains()
    assert drain(sessions, a) == 0  # nothing due: nothing queued


def test_a_feature_version_change_reanalyzes_and_neighbours_never_mix_versions(
    sessions: sessionmaker[Session], ops: FileOps
) -> None:
    ids = add_tracks(sessions, "1", "2", "3")
    make(sessions, ops, version=1).start_chains()
    drain(sessions, make(sessions, ops, version=1))
    with sessions() as s:
        assert [t for t, _ in similarity.nearest(s, [ids[0]], version=1)] == [ids[1], ids[2]]
    v2 = make(sessions, ops, version=2)
    v2.analyze_one(ids[2])  # one track at the new version
    with sessions() as s:
        assert similarity.nearest(s, [ids[0]], version=2) == []  # its seed is still version 1
        assert similarity.nearest(s, [ids[2]], version=2) == []  # nothing else at version 2
        assert [t for t, _ in similarity.nearest(s, [ids[0]], version=1)] == [ids[1]]
        assert set(similarity.analyses(s, ids, version=1)) == {ids[0], ids[1]}
    v2.start_chains()  # the version change queues re-analysis of the rest
    assert drain(sessions, v2) == 2
    assert {r.feature_version for r in rows(sessions).values()} == {2}
    with sessions() as s:
        assert similarity.nearest(s, [ids[0]], k=1, version=2) == [(ids[1], 1.0)]
        assert similarity.nearest(s, [ids[0], ids[2]], version=2) == [(ids[1], 0.0)]  # centroid
        got = similarity.analyses(s, [ids[0]], version=2)[ids[0]]
        assert (got.bpm, got.key_tonic, got.key_mode, got.energy) == (120.0, "C", "major", -12.0)


def test_a_failure_is_recorded_and_not_retried(
    sessions: sessionmaker[Session], ops: FileOps
) -> None:
    [bad, good] = add_tracks(sessions, "bad", "2")
    seen: list[str | None] = []

    def flaky(path: Path) -> Features:
        with sessions() as s:  # recorded before the work: a crash here is never retried
            seen.append(s.get_one(TrackAnalysis, bad).failure)
        return fake(path)  # float("bad") raises

    a = make(sessions, ops, analyze=flaky)
    a.analyze_one(bad)
    assert seen == [analysis.UNFINISHED]
    a.start_chains()
    assert drain(sessions, a) == 1 and len(seen) == 2  # only the good track was due
    a.analyze_one(bad)  # an import's job for it again: done at this version, so skipped
    got = rows(sessions)
    assert "ValueError" in (got[bad].failure or "") and got[good].failure is None
    with sessions() as s:
        assert analysis.counts(s) == (1, 1, 2)
        assert similarity.analyses(s, [bad, good]).keys() == {good}
    a.start_chains()
    assert drain(sessions, a) == 0  # the failed track is never queued again at this version


def test_a_raising_analyzer_records_its_error(
    sessions: sessionmaker[Session], ops: FileOps
) -> None:
    [tid] = add_tracks(sessions, "nope")
    make(sessions, ops).analyze_one(tid)
    failure = rows(sessions)[tid].failure
    assert failure and "ValueError" in failure and rows(sessions)[tid].vector is None


def test_with_analysis_off_nothing_is_queued_or_run(
    sessions: sessionmaker[Session],
    ops: FileOps,
    make_src: MakeSrc,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIXSYNC_AUDIO_ANALYSIS", "false")
    settings = Settings()
    assert settings.audio_analysis is False and Settings.model_fields["audio_analysis"].default
    importer = Importer(
        ops, sessions, SystemClock(), settings.path_template, settings.audio_analysis
    )
    importer.import_release([(make_src("flac", "a"), T1, None)], ALBUM, "verified", "b")
    add_tracks(sessions, "1")
    a = make(sessions, ops, enabled=settings.audio_analysis)
    a.start_chains()
    assert jobs(sessions) == []
    with sessions.begin() as s:  # queued before the switch went off
        enqueue_in(s, datetime.now(UTC), JobKind.ANALYZE, {}, priority=ANALYZE_PRIORITY)
    [job] = jobs(sessions)
    asyncio.run(a.run(JobQueue(engine_of(sessions), SystemClock(), lambda *_: None).get(job.id)))
    assert rows(sessions) == {} and len(jobs(sessions)) == 1


def test_a_removed_track_drops_its_analysis(sessions: sessionmaker[Session], ops: FileOps) -> None:
    keep, gone = add_tracks(sessions, "1", "2")
    a = make(sessions, ops)
    a.analyze_one(keep)
    a.analyze_one(gone)
    with sessions.begin() as s:  # as a trash op removes the row (acquire.merge)
        s.delete(s.get_one(Track, gone))
    assert set(rows(sessions)) == {keep}
    with sessions() as s:
        assert analysis.counts(s) == (1, 0, 1)
        assert [t for t, _ in similarity.nearest(s, [keep])] == []


def test_no_live_job_is_left_behind_while_tracks_are_due(
    sessions: sessionmaker[Session], ops: FileOps
) -> None:
    """An import's job finishing last continues the backfill."""
    first, second = add_tracks(sessions, "1", "2")
    with sessions.begin() as s:
        analysis.enqueue_analysis(s, datetime.now(UTC), first)
    a = make(sessions, ops)
    assert drain(sessions, a) == 2
    with sessions() as s:
        assert s.scalar(select(Job.id).where(Job.status.in_(LIVE))) is None
    assert set(rows(sessions)) == {first, second}


def test_two_jobs_finishing_together_keep_the_backfill_going(
    sessions: sessionmaker[Session], ops: FileOps
) -> None:
    """Two workers: each job ends while the other still runs; neither may end the chain."""
    ids = add_tracks(sessions, "1", "2", "3", "4")
    with sessions.begin() as s:
        for _ in range(2):
            enqueue_in(s, datetime.now(UTC), JobKind.ANALYZE, {}, priority=ANALYZE_PRIORITY)
    queue = JobQueue(engine_of(sessions), SystemClock(), lambda *_: None)
    a = make(sessions, ops)
    claimed = [queue.claim(w) for w in ("w1", "w2")]
    for job, w in zip(claimed, ("w1", "w2"), strict=True):
        assert job
        queue.start(job.id, w)
    for job in claimed:  # both handlers run before either job completes
        assert job
        a.analyze_one(job.payload.get("track_id"))
    for job, w in zip(claimed, ("w1", "w2"), strict=True):
        assert job
        queue.complete(job.id, w)
    with sessions() as s:  # two tracks are still due, so a job must be waiting for them
        assert s.scalar(select(Job.id).where(Job.status.in_(LIVE))) is not None
    drain(sessions, a)
    assert set(rows(sessions)) == set(ids)


def test_a_job_that_loses_its_track_to_another_worker_continues_the_chain(
    sessions: sessionmaker[Session], ops: FileOps, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = add_tracks(sessions, "1", "2")
    record = AudioAnalysis._record  # pyright: ignore[reportPrivateUsage]
    lost: list[int] = []

    def racing(self: AudioAnalysis, s: Session, track_id: int, *rest: object) -> None:
        if not lost:  # the other worker inserted this track's row first
            lost.append(track_id)
            raise IntegrityError("INSERT INTO track_analyses", None, Exception("duplicate"))
        record(self, s, track_id, *rest)  # type: ignore[arg-type]

    monkeypatch.setattr(AudioAnalysis, "_record", racing)
    a = make(sessions, ops)
    a.analyze_one(None)  # no error: the job ends well and queues the next one
    assert lost == [ids[0]] and len(jobs(sessions)) == 1
    assert drain(sessions, a) == 2  # the fake race wrote no row, so both tracks are still due
    assert set(rows(sessions)) == set(ids)


class FakeBliss(ModuleType):
    """A stand-in for the bliss_audio extension, which only the image has."""

    def __init__(self, vector: list[float]) -> None:
        super().__init__("bliss_audio")
        self.vector, self.calls = vector, 0
        bliss = self

        class Song:
            def __init__(self, path: str) -> None:
                bliss.calls += 1
                self.analysis = np.array(bliss.vector, dtype=np.float32)

        self.Song = Song


def test_a_bliss_vector_is_stored_and_stable(
    sessions: sessionmaker[Session], ops: FileOps, monkeypatch: pytest.MonkeyPatch
) -> None:
    bliss = FakeBliss([0.5, -0.25, 1.0])
    monkeypatch.setitem(sys.modules, "bliss_audio", bliss)
    triad(ops.library / "t.wav", 261.63, 329.63, 392.0)
    [tid] = add_tracks(sessions, "t")
    with sessions.begin() as s:
        s.get_one(Track, tid).path = "t.wav"
    first = analyze_file(ops.library / "t.wav")
    assert first == analyze_file(ops.library / "t.wav") and first.vector == [0.5, -0.25, 1.0]
    make(sessions, ops, analyze=analyze_file).analyze_one(tid)
    got = rows(sessions)[tid]
    assert got.failure is None and got.vector == [0.5, -0.25, 1.0] and bliss.calls == 3


def test_a_non_finite_bliss_feature_is_recorded_as_a_failure(
    sessions: sessionmaker[Session], ops: FileOps, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "bliss_audio", FakeBliss([0.5, float("nan")]))
    triad(ops.library / "t.wav", 261.63, 329.63, 392.0)
    [tid] = add_tracks(sessions, "t")
    with sessions.begin() as s:
        s.get_one(Track, tid).path = "t.wav"
    make(sessions, ops, analyze=analyze_file).analyze_one(tid)
    got = rows(sessions)[tid]
    assert "non-finite" in (got.failure or "") and got.vector is None and got.bpm is None


def test_a_track_longer_than_the_limit_gets_no_bliss_vector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bliss = FakeBliss([1.0, 2.0])
    monkeypatch.setitem(sys.modules, "bliss_audio", bliss)
    monkeypatch.setattr(analysis, "MAX_SECONDS", 5)
    long = analyze_file(triad(tmp_path / "long.wav", 220.0, 261.63, 329.63, seconds=8))
    assert long.vector is None and bliss.calls == 0  # bliss would decode all of it
    assert (long.key_tonic, long.key_mode) == ("A", "minor")  # the first 5 s are analyzed
    short = analyze_file(triad(tmp_path / "short.wav", 220.0, 261.63, 329.63, seconds=4))
    assert short.vector == [1.0, 2.0] and bliss.calls == 1
