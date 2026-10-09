import itertools
import re

import pytest

from mixsync.core.matching import AlbumInfo, Band, FileTrack, Likelies, TrackInfo
from mixsync.match.profiles import BALANCED
from mixsync.match.scorer import (
    DEFAULT_WEIGHTS,
    SCORER_VERSION,
    Distance,
    album_distance,
    assign_tracks,
    score_files,
    track_distance,
)

W = {**DEFAULT_WEIGHTS, "album": 4.0, "medium": 2.0}


def dist() -> Distance:
    return Distance(W)


def test_add_methods() -> None:
    d = dist()
    d.add_equality("album", "ghi", ["abc", "def", "ghi"], "r")
    d.add_equality("album", "xyz", ["abc", "def", "ghi"], "r")
    d.add_equality("album", "abc", re.compile("ABC", re.I), "r")
    assert [p for p, _ in d._penalties["album"]] == [0.0, 1.0, 0.0]  # pyright: ignore[reportPrivateUsage]

    d = dist()
    d.add_expr("media", True, "r")
    d.add_expr("media", False, "r")
    d.add_number("media", 1, 1, "r")
    d.add_number("media", 2, 1, "r")
    d.add_number("media", -1, 2, "r")
    assert [p for p, _ in d._penalties["media"]] == [1, 0, 0, 1, 1, 1, 1]  # pyright: ignore[reportPrivateUsage]

    d = dist()
    d.add_ratio("year", 25, 100, "r")
    d.add_ratio("year", 10, 5, "r")
    d.add_ratio("year", -5, 5, "r")
    d.add_ratio("year", 5, 0, "r")
    assert [p for p, _ in d._penalties["year"]] == [0.25, 1.0, 0.0, 0.0]  # pyright: ignore[reportPrivateUsage]

    d = dist()
    d.add_string("label", "abc", "bcd", "r")
    d.add_string("label", "abc", None, "r")
    d.add_string("label", None, None, "r")
    assert [p for p, _ in d._penalties["label"]] == [2 / 3, 1, 0]  # pyright: ignore[reportPrivateUsage]


def test_add_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        dist().add("album", 1.5, "r")


def test_distance_math_and_breakdown() -> None:
    d = dist()
    d.add("album", 0.5, "a")
    d.add("media", 0.25, "m1")
    d.add("media", 0.75, "m2")
    d.add("label", 0.0, "fine")
    assert d.max_distance == 4 + 2 * 1 + 0.5
    assert d.raw_distance == 2 + 1 + 0
    assert d.distance == pytest.approx(3 / 6.5)
    b = {p.key: p for p in d.breakdown()}
    assert set(b) == {"album", "media"}  # zero penalties are omitted
    assert (b["album"].penalty, b["album"].weight, b["album"].reason) == (0.5, 4.0, "a")
    assert (b["media"].penalty, b["media"].weight) == (0.5, 2.0)
    assert b["media"].reason == "m1; m2"


def test_empty_distance_is_zero() -> None:
    assert dist().distance == 0.0


def file(title: str, track: int, **kw: object) -> FileTrack:
    return FileTrack(title=title, artist="artist", length=100.0, track=track, **kw)  # pyright: ignore[reportArgumentType]


def info(title: str, index: int, **kw: object) -> TrackInfo:
    return TrackInfo(title=title, artist="artist", length=100.0, index=index, **kw)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("title", "artist", "penalized"),
    [
        ("title", "artist", False),
        ("title", "Various Artists", False),
        ("title", "different artist", True),
        ("different title", "artist", True),
    ],
)
def test_track_distance(title: str, artist: str, penalized: bool) -> None:
    f = FileTrack(title=title, artist=artist)
    t = TrackInfo(title="title", artist="artist")
    assert bool(track_distance(f, t, incl_artist=True).breakdown()) == penalized


def test_track_length_grace_and_max() -> None:
    t = info("x", 1)
    assert track_distance(file("x", 1), t).distance == 0
    within = FileTrack("x", "artist", length=109.0, track=1)
    assert track_distance(within, t).distance == 0
    way_off = FileTrack("x", "artist", length=200.0, track=1)
    (p,) = track_distance(way_off, t).breakdown()
    assert p.key == "track_length" and p.penalty == 1.0


def test_track_index_accepts_per_disc_numbering() -> None:
    t = info("x", 12, medium_index=2, medium=2)
    f = FileTrack("x", "artist", length=100.0, track=2, disc=2)
    assert track_distance(f, t).distance == 0
    assert track_distance(FileTrack("x", "artist", length=100.0, track=5), t).breakdown()


def test_track_recording_id_mismatch_is_penalized() -> None:
    t = info("x", 1, recording_id="a")
    assert track_distance(file("x", 1, recording_id="a"), t).distance == 0
    (p,) = track_distance(file("x", 1, recording_id="b"), t).breakdown()
    assert p.key == "track_id"


def album(*titles: str, va: bool = False) -> AlbumInfo:
    return AlbumInfo(
        artist="artist",
        album="album",
        va=va,
        tracks=tuple(info(t, i) for i, t in enumerate(titles, 1)),
    )


LIKE = Likelies(artist="artist", album="album")
FILES = [file("one", 1), file("two", 2), file("three", 3)]


def adist(files: list[FileTrack], a: AlbumInfo) -> float:
    pairs, extra, _ = assign_tracks(files, a.tracks)
    return album_distance(LIKE, a, pairs, len(extra)).distance


def test_identical_album() -> None:
    assert adist(FILES, album("one", "two", "three")) == 0


def test_incomplete_album() -> None:
    assert 0 < adist(FILES, album("one", "two")) < 0.2


def test_overly_complete_album() -> None:
    assert 0 < adist(FILES, album("one", "two", "three", "four")) < 0.2


@pytest.mark.parametrize("va", [True, False])
def test_album_artist(va: bool) -> None:
    a = AlbumInfo(
        artist="another artist",
        album="album",
        va=va,
        tracks=tuple(info(t, i) for i, t in enumerate(("one", "two", "three"), 1)),
    )
    assert bool(adist(FILES, a)) is not va


def test_tracks_out_of_order() -> None:
    assert 0 < adist(FILES, album("one", "three", "two")) < 0.2


def year_penalty(likelies: Likelies, a: AlbumInfo) -> float | None:
    d = album_distance(likelies, a, [], 0, this_year=2020)
    return next(
        (p.penalty for p in d.breakdown() if p.key == "year"),
        0.0 if "year" in d._penalties else None,
    )  # pyright: ignore[reportPrivateUsage]


def test_year_rules() -> None:
    a = AlbumInfo("artist", "album", (), year=2000, original_year=1990)
    assert year_penalty(Likelies(year=2000), a) == 0.0
    assert year_penalty(Likelies(year=1990), a) == 0.0
    assert year_penalty(Likelies(year=1995), a) == pytest.approx(5 / 30)
    assert year_penalty(Likelies(), a) is None
    no_orig = AlbumInfo("artist", "album", (), year=2000)
    assert year_penalty(Likelies(year=1995), no_orig) == 1.0


def test_release_id_mismatch() -> None:
    a = AlbumInfo("artist", "album", (), release_id="r1")

    def keys(likelies: Likelies) -> set[str]:
        return {p.key for p in album_distance(likelies, a, [], 0).breakdown()}

    assert "album_id" not in keys(Likelies(release_id="r1"))
    assert "album_id" in keys(Likelies(release_id="r2"))


def test_assignment_picks_optimal_permutation() -> None:
    titles = ["alpha", "bravo", "charlie", "delta", "echo"]
    tracks = tuple(TrackInfo(title=t) for t in titles)
    for perm in itertools.islice(itertools.permutations(titles), 0, 120, 7):
        files = [FileTrack(title=t, artist="a") for t in perm]
        pairs, extra_f, extra_t = assign_tracks(files, tracks)
        assert [(f.title, t.title) for f, t in pairs] == [(t, t) for t in perm]
        assert not extra_f and not extra_t


def test_assignment_beats_greedy() -> None:
    # Greedy file-by-file would give the first file its best track and strand the second.
    tracks = (TrackInfo(title="abcd"), TrackInfo(title="abxx"))
    files = [FileTrack(title="abcx", artist="a"), FileTrack(title="abxx", artist="a")]
    pairs, _, _ = assign_tracks(files, tracks)
    assert [(f.title, t.title) for f, t in pairs] == [("abcx", "abcd"), ("abxx", "abxx")]


def test_assignment_extra_files() -> None:
    tracks = (TrackInfo(title="one"), TrackInfo(title="two"))
    files = [FileTrack("zzz", "a"), FileTrack("two", "a"), FileTrack("one", "a")]
    pairs, extra_f, extra_t = assign_tracks(files, tracks)
    assert [(f.title, t.title) for f, t in pairs] == [("two", "two"), ("one", "one")]
    assert [f.title for f in extra_f] == ["zzz"]
    assert extra_t == []


def test_assignment_missing_tracks() -> None:
    tracks = tuple(TrackInfo(title=t) for t in ("one", "two", "three"))
    files = [FileTrack("three", "a"), FileTrack("one", "a")]
    pairs, extra_f, extra_t = assign_tracks(files, tracks)
    assert [(f.title, t.title) for f, t in pairs] == [("three", "three"), ("one", "one")]
    assert extra_f == []
    assert [t.title for t in extra_t] == ["two"]


def test_assignment_empty() -> None:
    assert assign_tracks([], (TrackInfo(title="x"),)) == ([], [], [TrackInfo(title="x")])
    f = FileTrack("x", "a")
    assert assign_tracks([f], ()) == ([], [f], [])


def test_score_files_perfect_auto_accepts() -> None:
    r = score_files(FILES, LIKE, album("one", "two", "three"), BALANCED)
    assert r.distance == 0 and r.band == Band.AUTO_ACCEPT
    assert r.breakdown == () and r.vetoes == () and r.scorer_version == SCORER_VERSION


def test_score_files_veto_forces_review() -> None:
    files = [file("one", 1), file("two", 2), FileTrack("three", "artist", length=160.0, track=3)]
    r = score_files(files, LIKE, album("one", "two", "three"), BALANCED)
    assert r.vetoes and r.band == Band.REVIEW


def test_score_files_garbage_rejects() -> None:
    files = [FileTrack("qqq", "zzz", track=9)]
    r = score_files(files, Likelies(artist="zzz", album="qqq"), album("one", "two"), BALANCED)
    assert r.band == Band.REJECT and r.breakdown
