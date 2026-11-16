"""Read side of audio analysis (ADR 0025), for arc order and Radio (#338).

Only successful analyses at one feature version (the current one by default) are returned, so
values and vectors from different analyzer versions never mix. A track that isn't analyzed yet
is simply absent. Nearest neighbours are brute force over every vector of that version: fine at
home-library scale (tens of thousands of tracks)."""

import heapq
import math
from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from mixsync.db.models.library import TrackAnalysis
from mixsync.library.analysis import FEATURE_VERSION


@dataclass(frozen=True)
class Analysis:
    track_id: int
    bpm: float | None
    key_tonic: str | None  # C, C#, ..., B
    key_mode: str | None  # major | minor
    energy: float | None  # RMS loudness, dBFS
    vector: list[float] | None  # None where bliss isn't installed


def _ok(version: int) -> tuple[ColumnElement[bool], ...]:
    return (TrackAnalysis.feature_version == version, TrackAnalysis.failure.is_(None))


def analyses(
    s: Session, track_ids: Collection[int], version: int = FEATURE_VERSION
) -> dict[int, Analysis]:
    """The analysis of each given track that has one at `version`, by track id."""
    rows = s.scalars(
        select(TrackAnalysis).where(TrackAnalysis.track_id.in_(track_ids), *_ok(version))
    )
    return {
        r.track_id: Analysis(r.track_id, r.bpm, r.key_tonic, r.key_mode, r.energy, r.vector)
        for r in rows
    }


def nearest(
    s: Session, seeds: Collection[int], k: int = 25, version: int = FEATURE_VERSION
) -> list[tuple[int, float]]:
    """The k tracks closest to the seeds' mean vector, as (track id, euclidean distance),
    closest first. Seeds are excluded; seeds without a vector at `version` are ignored, and
    with none left the result is empty."""
    rows = s.execute(
        select(TrackAnalysis.track_id, TrackAnalysis.vector).where(
            TrackAnalysis.vector.is_not(None), *_ok(version)
        )
    ).all()
    seeded = [v for t, v in rows if t in seeds and v]
    if not seeded:
        return []
    centre = [sum(c) / len(seeded) for c in zip(*seeded, strict=True)]
    others = ((t, math.dist(centre, v)) for t, v in rows if t not in seeds and v)
    return heapq.nsmallest(k, others, key=lambda p: p[1])
