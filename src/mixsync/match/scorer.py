# Ported from beets (https://github.com/beetbox/beets) at commit
# b8c9661fe10463cc761b83f8efd861cb49224224: beets/autotag/distance.py,
# beets/autotag/match.py and beets/config_default.yaml.
#
# The MIT License
#
# Copyright (c) 2010-2016 Adrian Sampson
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
"""Distance between a batch of files and a MusicBrainz release."""

import datetime
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from mixsync.core.matching import (
    AlbumInfo,
    FileTrack,
    Likelies,
    MatchResult,
    Penalty,
    Profile,
    TrackInfo,
)
from mixsync.match.features import string_dist
from mixsync.match.profiles import band
from mixsync.match.vetoes import find_vetoes

SCORER_VERSION = 2  # 2: stage-1 candidate scoring

# Artist values that mean "various artists".
VA_ARTISTS = ("", "various artists", "various", "va", "unknown")

# Seconds of length difference that cost nothing, and the further difference that costs 1.0.
TRACK_LENGTH_GRACE = 10.0
TRACK_LENGTH_MAX = 30.0

DEFAULT_WEIGHTS: Mapping[str, float] = MappingProxyType(
    {
        "artist": 3.0,
        "album": 3.0,
        "media": 1.0,
        "mediums": 1.0,
        "year": 1.0,
        "country": 0.5,
        "label": 0.5,
        "catalognum": 0.5,
        "albumdisambig": 0.5,
        "album_id": 5.0,
        "tracks": 2.0,
        "missing_tracks": 0.9,
        "unmatched_tracks": 0.6,
        "track_title": 3.0,
        "track_artist": 2.0,
        "track_index": 1.0,
        "track_length": 2.0,
        "track_id": 5.0,
        "medium": 1.0,
    }
)


class Distance:
    """Collects penalties in [0, 1] per key; the distance is their weighted mean."""

    def __init__(self, weights: Mapping[str, float] = DEFAULT_WEIGHTS) -> None:
        self._weights = weights
        self._penalties: dict[str, list[tuple[float, str]]] = {}

    @property
    def max_distance(self) -> float:
        return sum(len(p) * self._weights[k] for k, p in self._penalties.items())

    @property
    def raw_distance(self) -> float:
        return sum(sum(d for d, _ in p) * self._weights[k] for k, p in self._penalties.items())

    @property
    def distance(self) -> float:
        dist_max = self.max_distance
        return self.raw_distance / dist_max if dist_max else 0.0

    def breakdown(self) -> tuple[Penalty, ...]:
        """Keys with a non-zero penalty, largest share of the distance first."""
        out = [
            Penalty(
                key=k,
                penalty=sum(d for d, _ in p) / len(p),
                weight=self._weights[k] * len(p),
                reason="; ".join(r for d, r in p if d),
            )
            for k, p in self._penalties.items()
            if any(d for d, _ in p)
        ]
        return tuple(sorted(out, key=lambda x: (-x.penalty * x.weight, x.key)))

    def add(self, key: str, dist: float, reason: str) -> None:
        if not 0.0 <= dist <= 1.0:
            raise ValueError(f"`dist` must be between 0.0 and 1.0, not {dist}")
        self._penalties.setdefault(key, []).append((dist, reason))

    def add_equality(
        self,
        key: str,
        value: str | None,
        options: str | re.Pattern[str] | Sequence[str] | None,
        reason: str,
    ) -> None:
        """Penalty 1.0 unless `value` equals `options` (or any of them, if a list or tuple).
        A compiled regex option matches against `value`."""
        opts = (
            options if isinstance(options, Sequence) and not isinstance(options, str) else [options]
        )
        for opt in opts:
            if isinstance(opt, re.Pattern):
                hit = value is not None and bool(opt.match(value))
            else:
                hit = opt == value
            if hit:
                self.add(key, 0.0, reason)
                return
        self.add(key, 1.0, reason)

    def add_expr(self, key: str, expr: bool, reason: str) -> None:
        self.add(key, 1.0 if expr else 0.0, reason)

    def add_number(self, key: str, number1: int, number2: int, reason: str) -> None:
        """One 1.0 penalty per unit of difference, or a single 0.0 when equal."""
        diff = abs(number1 - number2)
        for _ in range(diff):
            self.add(key, 1.0, reason)
        if not diff:
            self.add(key, 0.0, reason)

    def add_ratio(self, key: str, number1: float, number2: float, reason: str) -> None:
        """Penalty `number1 / number2`, with `number1` clamped to [0, number2]."""
        number = float(max(min(number1, number2), 0))
        self.add(key, number / number2 if number2 else 0.0, reason)

    def add_string(self, key: str, str1: str | None, str2: str | None, reason: str) -> None:
        self.add(key, string_dist(str1, str2), reason)


def track_distance(
    file: FileTrack,
    track: TrackInfo,
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
    incl_artist: bool = False,
) -> Distance:
    """`incl_artist` adds a track artist penalty (for various-artist releases)."""
    dist = Distance(weights)

    if file.length and track.length:
        dist.add_ratio(
            "track_length",
            abs(file.length - track.length) - TRACK_LENGTH_GRACE,
            TRACK_LENGTH_MAX,
            f"length {file.length:.0f}s vs {track.length:.0f}s",
        )

    dist.add_string(
        "track_title", file.title, track.title, f"title {file.title!r} vs {track.title!r}"
    )

    if incl_artist and track.artist and file.artist.lower() not in VA_ARTISTS:
        dist.add_string(
            "track_artist",
            file.artist,
            track.artist,
            f"artist {file.artist!r} vs {track.artist!r}",
        )

    # Tolerates both per-disc and per-release numbering.
    if track.index and file.track:
        dist.add_expr(
            "track_index",
            file.track not in (track.medium_index, track.index),
            f"track number {file.track} vs {track.medium_index or track.index}",
        )

    if file.recording_id:
        dist.add_expr(
            "track_id",
            file.recording_id != track.recording_id,
            f"recording id {file.recording_id} vs {track.recording_id}",
        )

    if track.medium and file.disc:
        dist.add_expr("medium", file.disc != track.medium, f"disc {file.disc} vs {track.medium}")

    return dist


def _hungarian(cost: list[list[float]]) -> list[int]:
    """Min-cost assignment for an n x m matrix with n <= m; returns the column of each row."""
    n, m = len(cost), len(cost[0])
    inf = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)  # p[j]: row (1-based) assigned to column j
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], inf, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    cols = [0] * n
    for j in range(1, m + 1):
        if p[j]:
            cols[p[j] - 1] = j - 1
    return cols


def assign_tracks(
    files: Sequence[FileTrack],
    tracks: Sequence[TrackInfo],
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
) -> tuple[list[tuple[FileTrack, TrackInfo]], list[FileTrack], list[TrackInfo]]:
    """Best one-to-one mapping of files to tracks by track distance. Returns the pairs
    (in file order), the extra files and the missing tracks."""
    if not files or not tracks:
        return [], list(files), list(tracks)
    costs = [[track_distance(f, t, weights).distance for t in tracks] for f in files]
    if len(files) <= len(tracks):
        pairs = list(enumerate(_hungarian(costs)))
    else:
        by_track = _hungarian([list(col) for col in zip(*costs, strict=True)])
        pairs = sorted((fi, ti) for ti, fi in enumerate(by_track))
    used_f = {fi for fi, _ in pairs}
    used_t = {ti for _, ti in pairs}
    return (
        [(files[fi], tracks[ti]) for fi, ti in pairs],
        [f for i, f in enumerate(files) if i not in used_f],
        [t for i, t in enumerate(tracks) if i not in used_t],
    )


def album_distance(
    original: Likelies,
    album: AlbumInfo,
    pairs: Sequence[tuple[FileTrack, TrackInfo]],
    unmatched_count: int,
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
    this_year: int | None = None,
) -> Distance:
    """`pairs` are the matched (file, track); `unmatched_count` is the number of extra files."""
    this_year = this_year or datetime.date.today().year
    dist = Distance(weights)

    if not album.va:
        dist.add_string(
            "artist",
            original.artist,
            album.artist,
            f"artist {original.artist!r} vs {album.artist!r}",
        )
    dist.add_string(
        "album", original.album, album.album, f"album {original.album!r} vs {album.album!r}"
    )

    if album.media and original.media:
        dist.add_equality(
            "media", album.media, original.media, f"media {original.media!r} vs {album.media!r}"
        )

    if original.disctotal and album.mediums:
        dist.add_number(
            "mediums",
            original.disctotal,
            album.mediums,
            f"{original.disctotal} discs vs {album.mediums}",
        )

    if original.year and album.year:
        reason = f"year {original.year} vs {album.year}"
        if original.year in (album.year, album.original_year):
            dist.add("year", 0.0, reason)
        elif album.original_year:
            # Prefer matches closest to the release year.
            dist.add_ratio(
                "year",
                abs(original.year - album.year),
                abs(this_year - album.original_year),
                reason,
            )
        else:
            dist.add("year", 1.0, reason)

    if original.country and album.country:
        dist.add_string(
            "country",
            original.country,
            album.country,
            f"country {original.country!r} vs {album.country!r}",
        )
    if original.label and album.label:
        dist.add_string(
            "label", original.label, album.label, f"label {original.label!r} vs {album.label!r}"
        )
    if original.catalognum and album.catalognum:
        dist.add_string(
            "catalognum",
            original.catalognum,
            album.catalognum,
            f"catalog number {original.catalognum!r} vs {album.catalognum!r}",
        )
    if original.albumdisambig and album.albumdisambig:
        dist.add_string(
            "albumdisambig",
            original.albumdisambig,
            album.albumdisambig,
            f"disambiguation {original.albumdisambig!r} vs {album.albumdisambig!r}",
        )

    if original.release_id:
        dist.add_equality(
            "album_id",
            original.release_id,
            album.release_id,
            f"release id {original.release_id} vs {album.release_id}",
        )

    for file, track in pairs:
        td = track_distance(file, track, weights, album.va)
        why = "; ".join(p.reason for p in td.breakdown())
        dist.add("tracks", td.distance, f"{file.title!r} -> {track.title!r}: {why}")

    for _ in range(len(album.tracks) - len(pairs)):
        dist.add("missing_tracks", 1.0, "release track has no file")
    for _ in range(unmatched_count):
        dist.add("unmatched_tracks", 1.0, "file has no release track")

    return dist


def score_files(
    files: Sequence[FileTrack],
    likelies: Likelies,
    album: AlbumInfo,
    profile: Profile,
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
) -> MatchResult:
    pairs, extra_files, _ = assign_tracks(files, album.tracks, weights)
    dist = album_distance(likelies, album, pairs, len(extra_files), weights)
    vetoes = find_vetoes(pairs)
    return MatchResult(
        distance=dist.distance,
        band=band(dist.distance, vetoes, profile),
        breakdown=dist.breakdown(),
        vetoes=vetoes,
        scorer_version=SCORER_VERSION,
    )
