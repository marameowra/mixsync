import itertools
from dataclasses import replace

import pytest

from mixsync.core.matching import AlbumInfo, Band, FileTrack, Likelies, Profile, TrackInfo
from mixsync.core.types import AcoustIdResult
from mixsync.match.profiles import BALANCED, LOOSE, STRICT, close_gap
from mixsync.match.scorer import (
    DEFAULT_WEIGHTS,
    SCORER_VERSION,
    Distance,
    album_distance,
    artist_dist,
    artist_partial,
    assign_tracks,
    best_release,
    score_files,
    track_distance,
)
from mixsync.match.vetoes import find_vetoes

W = {**DEFAULT_WEIGHTS, "album": 4.0, "medium": 2.0}


def dist() -> Distance:
    return Distance(W)


def test_add_methods() -> None:
    d = dist()
    d.add_equality("album", "ghi", ["abc", "def", "ghi"], "r")
    d.add_equality("album", "xyz", ["abc", "def", "ghi"], "r")
    d.add_equality("album", "abc", "abc", "r")
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


@pytest.mark.parametrize(
    ("tag_year", "release_year", "original_year", "expected"),
    [
        (2019, 2020, 2020, 1.0),
        (2021, 2020, 2020, 1.0),
        (2020, 2020, 2020, 0.0),
        (2021, 2021, 2020, 0.0),
        (2020, 2021, 2020, 0.0),
        (2019, 2020, 2018, 0.5),
        (2021, 2022, 2022, 0.5),
    ],
)
def test_year_penalty_at_current_year_boundary(
    tag_year: int, release_year: int, original_year: int, expected: float
) -> None:
    a = AlbumInfo("artist", "album", (), year=release_year, original_year=original_year)
    original = Likelies(artist="artist", album="album", year=tag_year)

    d = album_distance(original, a, [], 0, this_year=2020)

    if expected:
        (penalty,) = d.breakdown()
        assert penalty.key == "year"
        assert penalty.penalty == pytest.approx(expected)
        assert penalty.reason == f"year {tag_year} vs {release_year}"
    else:
        assert d.breakdown() == ()
    assert d.distance == pytest.approx(expected / 7)


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


def test_empty_tracklist_rejects_under_every_profile() -> None:
    for profile in (STRICT, BALANCED, LOOSE):
        r = score_files([], Likelies(), AlbumInfo("artist", "album", ()), profile)
        assert r.band == Band.REJECT and r.vetoes == ("release has no tracklist",)


@pytest.mark.parametrize(
    "credit",
    ["Alaska & Seba", "Alaska feat. Seba", "Alaska, Seba", "Alaska x Seba", "Alaska and Seba"],
)
def test_multi_artist_credit_is_partial_hit(credit: str) -> None:
    a = AlbumInfo(artist=credit, album="album", tracks=(info("one", 1),))
    like = Likelies(artist="Alaska", album="album")
    partial = album_distance(like, a, [(file("one", 1), a.tracks[0])], 0)
    miss = AlbumInfo(artist="Zorro", album="album", tracks=a.tracks)
    full_miss = album_distance(like, miss, [(file("one", 1), a.tracks[0])], 0)
    (pen,) = partial.breakdown()
    assert pen.key == "artist" and 0 < pen.penalty < 0.5
    assert partial.distance < full_miss.distance


def test_multi_artist_track_credit_is_partial_hit() -> None:
    f = FileTrack("one", "Alaska")
    t = TrackInfo(title="one", artist="Alaska & Seba")
    (pen,) = track_distance(f, t, incl_artist=True).breakdown()
    assert pen.key == "track_artist" and 0 < pen.penalty < 0.5


def metadata_only_band(present: int, total: int, profile: object = BALANCED) -> Band:
    full = album(*(f"t{i}" for i in range(1, total + 1)))
    files = [file(t.title, t.index) for t in full.tracks[:present]]
    return score_files(files, LIKE, full, profile).band  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("profile", [STRICT, BALANCED])
@pytest.mark.parametrize("present", [7, 6])
def test_missing_tracks_without_acoustid_do_not_auto_accept(present: int, profile: object) -> None:
    assert metadata_only_band(present, 10, profile) is not Band.AUTO_ACCEPT


def test_mostly_missing_release_without_acoustid_rejects() -> None:
    assert metadata_only_band(2, 10) is Band.REJECT


def test_one_of_thirteen_with_acoustid_auto_accepts() -> None:
    recs = [f"r{i}" for i in range(1, 14)]
    full = AlbumInfo(
        artist="artist",
        album="album",
        tracks=tuple(rec_track(f"t{i}", r) for i, r in enumerate(recs, 1)),
    )
    files = [sure_file("t1", "r1")]
    pairs, _, _ = assign_tracks(files, full.tracks)
    from mixsync.match.vetoes import all_confident

    assert all_confident(pairs)
    free = {**DEFAULT_WEIGHTS, "missing_tracks": 0.0}
    assert score_files(files, LIKE, full, BALANCED, free).band is Band.AUTO_ACCEPT


def test_standard_edition_beats_a_longer_deluxe_with_off_lengths() -> None:
    titles = [f"t{i}" for i in range(1, 20)]
    std = album(*titles)
    deluxe = replace(
        album(*titles, "bonus"),
        tracks=tuple(replace(t, length=112.0) for t in album(*titles, "bonus").tracks),
    )
    files = [file(t, i) for i, t in enumerate(titles, 1)]
    best, _ = best_release(files, LIKE, [deluxe, std], BALANCED)
    assert best is std


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("X", "Malcolm X"),
        ("Mr X", "X"),
        ("Xxx", "X"),
        ("Ft", "Ft. Lauderdale"),
        ("X", "Exene Cervenka & X"),
    ],
)
def test_artist_separator_words_never_crash(a: str, b: str) -> None:
    assert 0.0 <= artist_dist(a, b) <= 1.0
    assert artist_dist(b, a) == artist_dist(a, b)


def test_artist_word_separators_need_spaces() -> None:
    assert not artist_partial("X-Ray Spex", "X")
    assert artist_partial("Alaska", "Alaska & Seba")


@pytest.mark.parametrize(
    ("expected", "credit"),
    [
        ("Earth", "Earth, Wind & Fire"),
        ("Hall", "Hall & Oates"),
        ("Sebastian", "Belle and Sebastian"),
    ],
)
def test_partial_artist_credit_never_auto_accepts_on_tags_alone(expected: str, credit: str) -> None:
    a = AlbumInfo(artist=credit, album="album", tracks=(info("one", 1),))
    like = Likelies(artist=expected, album="album")
    assert score_files([file("one", 1)], like, a, LOOSE).band is Band.REVIEW


def test_partial_artist_credit_auto_accepts_with_acoustid() -> None:
    t = rec_track("one", "r1")
    a = AlbumInfo(artist="Alaska & Seba", album="album", tracks=(t,))
    like = Likelies(artist="Alaska", album="album")
    f = sure_file("one", "r1", track=None)
    assert score_files([f], like, a, BALANCED).band is Band.AUTO_ACCEPT


def sure_file(title: str, rec: str, score: float = 0.95, **kw: object) -> FileTrack:
    return FileTrack(  # pyright: ignore[reportArgumentType]
        title=title,
        artist="artist",
        length=100.0,
        acoustid=(AcoustIdResult("a", score, (rec,)),),
        **kw,
    )


def rec_track(title: str, rec: str) -> TrackInfo:
    return TrackInfo(title=title, artist="artist", length=100.0, recording_id=rec)


def test_acoustid_confirmed_track_discounts_metadata_differences() -> None:
    t = rec_track("Real Title", "r1")
    sure = track_distance(sure_file("Compl3tely different", "r1"), t).distance
    unsure = track_distance(sure_file("Compl3tely different", "r1", score=0.5), t).distance
    assert sure < unsure / 3


def test_acoustid_contradiction_counts_strongly_and_vetoes() -> None:
    f, t = sure_file("same", "r1"), rec_track("same", "r2")
    d = track_distance(f, t)
    assert d.distance > 0.3 and "track_id" in [p.key for p in d.breakdown()]
    assert find_vetoes([(f, t)])


def test_acoustid_overrides_a_stale_tagged_recording_id_but_not_duration() -> None:
    f = sure_file("x", "r1", recording_id="stale")
    t = rec_track("x", "r1")
    assert find_vetoes([(f, t)]) == ()
    long = replace(t, length=200.0)
    assert find_vetoes([(f, long)])


def test_no_acoustid_leaves_scoring_unchanged() -> None:
    f = FileTrack("Compl3tely different", "artist", length=100.0)
    d = track_distance(f, rec_track("Real Title", "r1"))
    assert d.distance == track_distance(f, rec_track("Real Title", "other")).distance
    assert d.distance > 0.3


def test_release_with_confirmed_recordings_outranks_tag_only_match() -> None:
    files = [sure_file("One", "r1", track=1)]
    tags = AlbumInfo(artist="artist", album="album", tracks=(rec_track("One", "x1"),))
    holds = AlbumInfo(artist="artist", album="album (Deluxe)", tracks=(rec_track("One", "r1"),))
    like = Likelies(artist="artist", album="album")
    best, _ = best_release(files, like, [tags, holds], BALANCED)
    assert best is holds


def test_among_releases_with_the_same_recordings_tags_decide() -> None:
    files = [sure_file("One", "r1", track=1)]
    comp = AlbumInfo(artist="Various Artists", album="Hits 2001", tracks=(rec_track("One", "r1"),))
    original = AlbumInfo(artist="artist", album="album", tracks=(rec_track("One", "r1"),))
    like = Likelies(artist="artist", album="album")
    for albums in ([comp, original], [original, comp]):
        assert best_release(files, like, albums, BALANCED)[0] is original


def test_compilation_with_more_confirmations_does_not_beat_the_tagged_album() -> None:
    recs = [f"r{i}" for i in range(1, 11)]
    files = [sure_file(f"t{i}", r, track=i) for i, r in enumerate(recs, 1)]
    like = Likelies(artist="artist", album="Album")
    a_tracks = tuple(rec_track(f"t{i}", r) for i, r in enumerate(recs[:9], 1)) + (
        rec_track("t10", "sibling"),
    )
    a = AlbumInfo(artist="artist", album="Album", tracks=a_tracks)
    filler = tuple(rec_track(f"other{i}", f"o{i}") for i in range(20))
    b = AlbumInfo(
        artist="artist",
        album="The Very Best Of artist",
        tracks=tuple(rec_track(f"t{i}", r) for i, r in enumerate(recs, 1)) + filler,
    )
    for profile in (STRICT, BALANCED, LOOSE):
        assert best_release(files, like, [b, a], profile)[0] is a


@pytest.mark.parametrize("profile", [STRICT, BALANCED, LOOSE])
def test_close_gap_is_inclusive_at_the_limit(profile: Profile) -> None:
    limit = profile.auto_accept_max
    assert close_gap(limit, profile)
    assert close_gap(0.1 + 0.2 - 0.3 + limit, profile)  # float noise at the limit
    assert close_gap(limit - 0.001, profile)
    assert not close_gap(limit + 0.001, profile)


def test_a_close_runner_up_caps_the_best_at_review() -> None:
    files = [sure_file("One", "r1", track=1)]
    like = Likelies(artist="artist", album="album")
    a = AlbumInfo(artist="artist", album="album", release_id="a", tracks=(rec_track("One", "r1"),))
    b = replace(a, release_id="b")
    album, result = best_release(files, like, [a, b], STRICT)
    assert result.band is Band.REVIEW
    assert result.runner_up is not None and result.runner_up.gap == 0.0
    assert {album.release_id, result.runner_up.release_id} == {"a", "b"}
    assert best_release(files, like, [a], STRICT)[1].band is Band.AUTO_ACCEPT


def test_a_distant_runner_up_does_not_cap() -> None:
    files = [sure_file("One", "r1", track=1)]
    like = Likelies(artist="artist", album="album")
    a = AlbumInfo(artist="artist", album="album", release_id="a", tracks=(rec_track("One", "r1"),))
    b = AlbumInfo(
        artist="other", album="different", release_id="b", tracks=(rec_track("One", "x"),)
    )
    _, result = best_release(files, like, [a, b], LOOSE)
    assert result.band is Band.AUTO_ACCEPT and result.runner_up is None


def test_acoustid_separated_runner_up_does_not_cap() -> None:
    files = [sure_file("One", "r1", track=1)]
    like = Likelies(artist="artist", album="album")
    holds = AlbumInfo(
        artist="artist", album="album (Deluxe)", release_id="a", tracks=(rec_track("One", "r1"),)
    )
    tags = AlbumInfo(
        artist="artist",
        album="album",
        release_id="b",
        tracks=(replace(rec_track("One", "x"), recording_id=None),),
    )
    album, result = best_release(files, like, [holds, tags], LOOSE)
    assert album is holds and tags_closer(files, like, tags, holds)
    assert result.band is Band.AUTO_ACCEPT and result.runner_up is None


def tags_closer(files: list[FileTrack], like: Likelies, a: AlbumInfo, b: AlbumInfo) -> bool:
    return score_files(files, like, a, LOOSE).distance < score_files(files, like, b, LOOSE).distance
