"""Audio analysis of library tracks (ADR 0025): BPM, key, energy and a similarity vector, kept
in `track_analyses` only (never in tags). One track per ANALYZE job at the lowest priority.

Analysis reads library files and writes nothing to them. A file that can't be analyzed (or
kills the worker mid-analysis) keeps a failure row and is not tried again until the feature
version changes. The bliss similarity vector needs `bliss_audio`, built into the image from
source; where it isn't installed (macOS development) the vector is null."""

import asyncio
import importlib
import logging
import math
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import Clock
from mixsync.core.jobs import JobKind, JobState
from mixsync.db.models.library import Track, TrackAnalysis
from mixsync.db.models.work import Job
from mixsync.db.queue import JobRecord, enqueue_in

log = logging.getLogger(__name__)

# Bump when what is computed changes: every track is analyzed again, and vectors of different
# versions are never compared.
FEATURE_VERSION = 1
ANALYZE_PRIORITY = 100  # lower runs first; every other job uses 1 or less
SR = 22050
MAX_SECONDS = 600  # librosa reads at most this much of a track (a long mix stays bounded)
HOP = 256  # onset frames of ~11.6 ms: tempo lands within 2 BPM (512 is off by 3 at 120 BPM)
UNFINISHED = "analysis did not finish (the worker stopped or crashed)"
NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
# Krumhansl-Kessler key profiles, tonic first
MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


@dataclass(frozen=True)
class Features:
    bpm: float
    key_tonic: str | None  # None for silence
    key_mode: str | None
    energy: float  # RMS loudness, dBFS
    vector: list[float] | None  # None without bliss_audio


Analyze = Callable[[Path], Features]
_FIELDS = tuple(f.name for f in fields(Features))


def _bliss() -> ModuleType | None:
    try:
        return importlib.import_module("bliss_audio")
    except ImportError:
        return None


def analyzer_name() -> str:
    try:
        bliss = f"bliss-audio {version('bliss_audio')}"
    except PackageNotFoundError:
        bliss = "no bliss-audio"
    return f"{bliss} + librosa {version('librosa')}"


def _decode(path: Path) -> np.ndarray[Any, np.dtype[np.float32]]:
    """Mono float samples at SR, decoded by ffmpeg (every format the library holds): at most
    MAX_SECONDS and one more second, which tells a longer track."""
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-t", str(MAX_SECONDS + 1)]
        + ["-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
        capture_output=True,
        check=True,
        timeout=300,
    ).stdout
    y = np.frombuffer(out, dtype="<f4")
    if not y.size:
        raise ValueError("no audio decoded")
    return y


def detect_key(
    chroma: np.ndarray[Any, np.dtype[np.floating[Any]]],
) -> tuple[str | None, str | None]:
    """The tonic and mode whose key profile best matches a 12-bin mean chroma."""
    if not chroma.any():
        return None, None
    _, tonic, mode = max(
        (float(np.corrcoef(chroma, np.roll(profile, tonic))[0, 1]), tonic, mode)
        for mode, profile in (("major", MAJOR), ("minor", MINOR))
        for tonic in range(12)
    )
    return NAMES[tonic], mode


def analyze_file(path: Path) -> Features:
    import librosa  # slow to import (numba): only the worker that analyzes pays for it

    y = _decode(path)
    long = y.size > MAX_SECONDS * SR
    y = y[: MAX_SECONDS * SR]
    bpm = float(librosa.feature.tempo(y=y, sr=SR, hop_length=HOP)[0])
    tonic, mode = detect_key(librosa.feature.chroma_cqt(y=y, sr=SR).mean(axis=1))
    energy = 20 * math.log10(max(float(np.sqrt(np.mean(np.square(y)))), 1e-10))
    bliss = None if long else _bliss()  # bliss decodes the whole file: a long mix gets no vector
    vector = [float(v) for v in bliss.Song(str(path)).analysis] if bliss else None
    if vector and not all(map(math.isfinite, vector)):  # JSON (and Postgres) can't hold NaN
        raise ValueError("bliss returned a non-finite feature")
    return Features(round(bpm, 2), tonic, mode, round(energy, 2), vector)


def enqueue_analysis(s: Session, now: datetime, track_id: int) -> None:
    enqueue_in(
        s, now, JobKind.ANALYZE, {"track_id": track_id}, priority=ANALYZE_PRIORITY, max_attempts=2
    )


class AudioAnalysis:
    """ANALYZE jobs. A job with a track_id analyzes that track (queued at import); one without
    analyzes the next track due. A job that ends with tracks still due and no ANALYZE job waiting
    queues one more, so a library is backfilled one job at a time, never flooding the queue."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        clock: Clock,
        library: Path,
        enabled: bool,
        analyze: Analyze = analyze_file,
        version: int = FEATURE_VERSION,
    ) -> None:
        self.sessions, self.clock, self.library = sessions, clock, library
        self.enabled, self.analyze, self.version = enabled, analyze, version

    def handlers(self) -> dict[JobKind, Callable[[JobRecord], Awaitable[None]]]:
        return {JobKind.ANALYZE: self.run}

    def _due(self, s: Session, track_id: int | None = None) -> int | None:
        """The first track (or this one) with no analysis at this version; a failure counts as
        an analysis."""
        q = select(Track.id) if track_id is None else select(Track.id).where(Track.id == track_id)
        return s.scalar(
            q.outerjoin(TrackAnalysis, TrackAnalysis.track_id == Track.id)
            .where(
                or_(
                    TrackAnalysis.track_id.is_(None),
                    TrackAnalysis.feature_version != self.version,
                )
            )
            .order_by(Track.id)
            .limit(1)
        )

    def _continue(self, s: Session) -> None:
        # Only waiting jobs count: two running jobs that each counted the other would both end
        # the chain. So at most one chain per worker runs at a time.
        waiting = (JobState.QUEUED, JobState.RETRY_WAIT)
        live = select(Job.id).where(Job.kind == JobKind.ANALYZE, Job.status.in_(waiting))
        if s.scalar(live.limit(1)) is None and self._due(s) is not None:
            enqueue_in(s, self.clock.now(), JobKind.ANALYZE, {}, priority=ANALYZE_PRIORITY)

    def start_chains(self) -> None:
        """On startup: queue the backfill if tracks are due and no ANALYZE job is live."""
        if self.enabled:
            with self.sessions.begin() as s:
                self._continue(s)

    async def run(self, job: JobRecord) -> None:
        if self.enabled:  # off: a job queued before the switch does nothing
            await asyncio.to_thread(self.analyze_one, job.payload.get("track_id"))

    def analyze_one(self, track_id: int | None) -> None:
        try:
            with self.sessions.begin() as s:
                due = self._due(s, track_id)
                path = s.scalar(select(Track.path).where(Track.id == due)) if due else None
                if due is not None:  # recorded first: a crash mid-analysis is never retried
                    self._record(s, due, None, UNFINISHED)
        except IntegrityError:  # another worker's job took this track first: go on
            due = path = None
        if due is not None and path is not None:
            features, failure = None, None
            try:
                features = self.analyze(self.library / path)
            except Exception as e:
                log.warning("analysis of track %s (%s) failed: %r", due, path, e)
                failure = repr(e)[:500]
            with self.sessions.begin() as s:
                if s.get(Track, due) is not None:  # trashed meanwhile: its row went with it
                    self._record(s, due, features, failure)
        with self.sessions.begin() as s:
            self._continue(s)

    def _record(self, s: Session, track_id: int, f: Features | None, failure: str | None) -> None:
        values = asdict(f) if f else dict.fromkeys(_FIELDS)  # a failure clears older values
        s.merge(
            TrackAnalysis(
                track_id=track_id,
                analyzer=analyzer_name(),
                feature_version=self.version,
                analyzed_at=self.clock.now(),
                failure=failure,
                **values,
            )
        )


def counts(s: Session, feature_version: int = FEATURE_VERSION) -> tuple[int, int, int]:
    """(analyzed, failed, total) library tracks at this feature version."""
    q = select(func.count()).select_from(TrackAnalysis)
    q = q.where(TrackAnalysis.feature_version == feature_version)
    analyzed = s.scalar(q.where(TrackAnalysis.failure.is_(None))) or 0
    failed = s.scalar(q.where(TrackAnalysis.failure.is_not(None))) or 0
    return analyzed, failed, s.scalar(select(func.count()).select_from(Track)) or 0
