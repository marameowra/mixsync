from collections.abc import Callable
from dataclasses import replace

import pytest

from mixsync.core.matching import AlbumInfo, Band, Candidate, CandidateFile, MatchResult, TrackInfo
from mixsync.match.candidates import parse_name, rank, score_candidate
from mixsync.match.profiles import BALANCED
from mixsync.match.scorer import SCORER_VERSION

TITLES = ["Opening", "Second Song", "Third Song", "Closing"]
LENGTHS = [200.0, 240.0, 180.0, 300.0]
FOLDER = "Music\\Test Artist\\Test Album"

ALBUM = AlbumInfo(
    artist="Test Artist",
    album="Test Album",
    tracks=tuple(
        TrackInfo(t, length=ln, index=i + 1, medium_index=i + 1, medium=1)
        for i, (t, ln) in enumerate(zip(TITLES, LENGTHS, strict=True))
    ),
)
TWO_DISCS = replace(
    ALBUM,
    tracks=tuple(
        replace(t, medium=1 + i // 2, medium_index=1 + i % 2) for i, t in enumerate(ALBUM.tracks)
    ),
    mediums=2,
)


def flac(name: str, length: float, folder: str = FOLDER, size: int | None = None) -> CandidateFile:
    return CandidateFile(
        f"{folder}\\{name}.flac", size or int(length * 880 * 125), "flac", None, length, 16, 44100
    )


def album_files(
    titles: list[str] = TITLES, folder: str = FOLDER, numbered: bool = True
) -> list[CandidateFile]:
    return [
        flac(f"{i + 1:02d} {t}" if numbered else t, ln, folder)
        for i, (t, ln) in enumerate(zip(titles, LENGTHS, strict=True))
    ]


def cand(
    files: list[CandidateFile], folder: str = FOLDER, user: str = "peer-001", **kw: object
) -> Candidate:
    kw.setdefault("upload_speed", 900_000)
    return Candidate(user, folder, tuple(files), **kw)  # pyright: ignore[reportArgumentType]


def score(c: Candidate, album: AlbumInfo = ALBUM) -> MatchResult:
    return score_candidate(c, album, BALANCED)


def vetoed(r: MatchResult, text: str) -> bool:
    return any(text in v for v in r.vetoes)


def keys(r: MatchResult) -> set[str]:
    return {p.key for p in r.breakdown}


def mp3(name: str, length: float, bitrate: int = 320, size: int | None = None) -> CandidateFile:
    return CandidateFile(
        f"{FOLDER}\\{name}.mp3", size or int(length * bitrate * 125), "mp3", bitrate, length
    )


def check_clean(r: MatchResult) -> None:
    assert r.band is Band.AUTO_ACCEPT and r.distance == 0 and not r.vetoes
    assert r.scorer_version == SCORER_VERSION


def check_title_ok(r: MatchResult) -> None:
    assert r.band is Band.AUTO_ACCEPT and not r.vetoes
    assert "title_similarity" in keys(r) and r.distance < 0.05


def check_review_with(text: str) -> Callable[[MatchResult], None]:
    def check(r: MatchResult) -> None:
        assert r.band is Band.REVIEW and vetoed(r, text)

    return check


def check_rejected(r: MatchResult) -> None:
    assert r.band is Band.REJECT and r.vetoes and r.vetoes[0] in r.breakdown[0].reason


NFD = "Björk"
CASES = [
    pytest.param(cand(album_files()), check_clean, id="SK-F01"),
    pytest.param(
        cand(
            album_files(["Opening", "Second Song feat. Guest", "Third Song ft. X", "Closing"]),
        ),
        check_title_ok,
        id="SK-N03",
    ),
    pytest.param(
        cand(
            album_files(
                [
                    "Opening (Remastered 2011)",
                    "Second Song [Deluxe]",
                    "Third Song (Explicit)",
                    "Closing",
                ]
            )
        ),
        check_title_ok,
        id="SK-N04",
    ),
    pytest.param(
        cand(album_files()[:3]),
        check_review_with("1 of 4 tracks missing"),
        id="SK-F02",
    ),
    pytest.param(
        cand(
            [
                flac("1-01 Opening", 200, FOLDER),
                flac("1-02 Second Song", 240, FOLDER),
                flac("2-01 Third Song", 180, FOLDER),
                flac("2-02 Closing", 300, FOLDER),
            ]
        ),
        check_clean,
        id="SK-F05",
    ),
    pytest.param(cand(album_files(numbered=False)), check_clean, id="SK-F06-no-numbers"),
    pytest.param(
        cand([flac(f"{i + 1:02d}", ln) for i, ln in enumerate(LENGTHS)]),
        check_review_with("no title"),
        id="SK-N07",
    ),
    pytest.param(
        cand([flac(f"track {i + 1}", ln) for i, ln in enumerate(LENGTHS)]),
        check_review_with("no title"),
        id="SK-N07-track-word",
    ),
    pytest.param(
        cand([flac("Test Artist - Test Album", 920)], has_cue=True),
        check_rejected,
        id="SK-F09",
    ),
    pytest.param(
        cand(
            [
                flac("01 Opening", 200, size=3_000_000),  # ~120 kbps "FLAC"
                *album_files()[1:],
            ]
        ),
        check_review_with("transcode"),
        id="SK-Q01",
    ),
    pytest.param(
        cand(
            [
                mp3(f"{i + 1:02d} {t}", ln, 320)
                for i, (t, ln) in enumerate(zip(TITLES, LENGTHS, strict=True))
            ][:3]
            + [mp3("04 Closing", 300, 320, size=300 * 128 * 125)]
        ),
        check_review_with("stated 320 kbps"),
        id="SK-Q02",
    ),
    pytest.param(cand([]), check_rejected, id="no-audio"),
]


@pytest.mark.parametrize("c, check", CASES)
def test_stage1(c: Candidate, check: Callable[[MatchResult], None]) -> None:
    check(score(c))


def test_stage1_sk_n01_nfd_folder_matches() -> None:
    nfd = replace(ALBUM, artist="Björk")
    nfc = replace(ALBUM, artist="Björk")
    c = cand(album_files(folder="Music\\Björk\\Test Album"), folder="Music\\Björk\\Test Album")
    assert score(c, nfd).band is Band.AUTO_ACCEPT and score(c, nfc).band is Band.AUTO_ACCEPT


def test_stage1_sk_f03_non_audio_not_counted() -> None:
    # The adapter drops non-audio files; four audio files still make a complete album.
    r = score(cand(album_files()))
    assert "extra_files" not in keys(r) and "folder_completeness" not in keys(r)


def test_stage1_sk_f04_disc_subfolders() -> None:
    folder = "Music\\Test Artist\\Test Album"
    files = [
        flac("01 Opening", 200, folder + "\\CD1"),
        flac("02 Second Song", 240, folder + "\\CD1"),
        flac("01 Third Song", 180, folder + "\\Disc 2"),
        flac("02 Closing", 300, folder + "\\Disc 2"),
    ]
    r = score(cand(files, folder), TWO_DISCS)
    assert r.band is Band.AUTO_ACCEPT and not r.breakdown


def test_stage1_sk_f05_disc_track_numbering() -> None:
    files = [
        flac("1-01 Opening", 200),
        flac("1-02 Second Song", 240),
        flac("2-01 Third Song", 180),
        flac("2-02 Closing", 300),
    ]
    assert score(cand(files), TWO_DISCS).band is Band.AUTO_ACCEPT
    assert parse_name("1-01 Opening") == (1, 1, "Opening")
    assert parse_name("2-13 - Title") == (2, 13, "Title")


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("01 - Opening", (None, 1, "Opening")),
        ("01. Opening", (None, 1, "Opening")),
        ("01_Opening", (None, 1, "Opening")),
        ("01-Opening", (None, 1, "Opening")),
        ("01", (None, 1, "")),
        ("Track 7", (None, 7, "")),
        ("Test Artist - 03 - Third Song", (None, 3, "Third Song")),
        ("Test Artist - Third Song", (None, None, "Test Artist - Third Song")),
        ("1979", (None, None, "1979")),
    ],
)
def test_parse_name(stem: str, expected: tuple[int | None, int | None, str]) -> None:
    assert parse_name(stem) == expected


def test_stage1_sk_f06_mixed_formats_keep_the_preferred_one() -> None:
    files = album_files() + [
        mp3(f"{i + 1:02d} {t}", ln) for i, (t, ln) in enumerate(zip(TITLES, LENGTHS, strict=True))
    ]
    r = score(cand(files))
    assert "extra_files" not in keys(r) and "quality_fit" not in keys(r)
    assert r.band is Band.AUTO_ACCEPT


def test_stage1_quality_prefers_lossless() -> None:
    lossy = [
        mp3(f"{i + 1:02d} {t}", ln, 192)
        for i, (t, ln) in enumerate(zip(TITLES, LENGTHS, strict=True))
    ]
    ranked = rank(
        [cand(lossy, user="peer-001"), cand(album_files(), user="peer-002")], ALBUM, BALANCED
    )
    assert [c.username for c, _ in ranked] == ["peer-002", "peer-001"]


def test_stage1_sk_f02_incomplete_ranks_below_complete() -> None:
    ranked = rank(
        [cand(album_files()[:3], user="peer-001"), cand(album_files(), user="peer-002")],
        ALBUM,
        BALANCED,
    )
    assert [c.username for c, _ in ranked] == ["peer-002", "peer-001"]
    assert ranked[0][1].distance < ranked[1][1].distance
    assert ranked[1][1].breakdown[0].key == "folder_completeness"


def test_stage1_sk_n05_wrong_artist_ranks_last() -> None:
    other = "Music\\Someone Else\\Greatest Hits"
    ranked = rank(
        [
            cand(album_files(folder=other), other, "peer-001"),
            cand(album_files(), FOLDER, "peer-002"),
        ],
        ALBUM,
        BALANCED,
    )
    assert [c.username for c, _ in ranked] == ["peer-002", "peer-001"]
    assert {"folder_artist", "folder_album"} <= keys(ranked[1][1])


def test_stage1_sk_p02_healthy_peer_preferred() -> None:
    busy = cand(
        album_files(), user="peer-001", has_free_slot=False, queue_length=200, upload_speed=1000
    )
    healthy = cand(
        album_files(), user="peer-002", has_free_slot=True, queue_length=0, upload_speed=500_000
    )
    ranked = rank([busy, healthy], ALBUM, BALANCED)
    assert [c.username for c, _ in ranked] == ["peer-002", "peer-001"]
    assert "peer_health" in keys(ranked[1][1]) and "peer_health" not in keys(ranked[0][1])


def test_stage1_sk_p08_one_folder_per_peer() -> None:
    other = "Music\\Test Artist\\Test Album (bootleg)"
    cands = [
        cand(album_files()[:3], user="peer-001"),
        cand(album_files(), user="peer-001", folder=other),
        cand(album_files(), user="peer-002"),
    ]
    ranked = rank(cands, ALBUM, BALANCED)
    assert sorted(c.username for c, _ in ranked) == ["peer-001", "peer-002"]
    best = next(c for c, _ in ranked if c.username == "peer-001")
    assert len(best.files) == 4


def test_stage1_rejected_sort_last_and_duration_veto() -> None:
    single = cand([flac("x", 920)], user="peer-001", has_cue=True)
    ranked = rank([single, cand(album_files(), user="peer-002")], ALBUM, BALANCED)
    assert [r.band for _, r in ranked] == [Band.AUTO_ACCEPT, Band.REJECT]
    long = album_files()
    long[0] = flac("01 Opening", 260)
    assert vetoed(score(cand(long)), "duration off by")


def test_stage1_duration_and_missing_length() -> None:
    near = album_files()
    near[0] = flac("01 Opening", 205)  # within the grace period
    assert "duration_match" not in keys(score(cand(near)))
    unknown = [replace(f, duration=None) for f in album_files()]
    assert "duration_match" not in keys(score(cand(unknown)))


def test_stage1_ties_break_on_slot_queue_speed() -> None:
    files = album_files()
    cands = [
        cand(files, user="peer-001", has_free_slot=False, queue_length=0, upload_speed=900_000),
        cand(files, user="peer-002", queue_length=40, upload_speed=900_000),
        cand(files, user="peer-003", queue_length=10, upload_speed=100_000),
        cand(files, user="peer-004", queue_length=10, upload_speed=900_000),
    ]
    ranked = rank(cands, ALBUM, BALANCED)
    assert {r.distance for _, r in ranked[:3]} == {0.0}
    assert [c.username for c, _ in ranked] == ["peer-004", "peer-003", "peer-002", "peer-001"]
