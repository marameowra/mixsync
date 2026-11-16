from pathlib import Path

from mixsync.core.matching import TrackInfo
from mixsync.core.types import ReleaseRef
from mixsync.library.canonical import choose, split


def ref(rid: str, **kw: object) -> ReleaseRef:
    return ReleaseRef(rid, "t", "a", **kw)  # type: ignore[arg-type]


def test_media_comes_first() -> None:
    cd = ref("a", formats=("CD",), date="1990")
    digital = ref("b", formats=("Digital Media",), date="2020")
    vinyl = ref("c", formats=("Vinyl",), date="1980")
    assert choose([vinyl, cd, digital]) == "b"
    assert choose([vinyl, cd]) == "a"  # unlisted media is worst
    assert (
        choose([ref("a", formats=("Vinyl", "CD"), date="2000"), ref("b", formats=("Vinyl",))])
        == "a"
    )


def test_earliest_date_missing_last() -> None:
    f = ("CD",)
    assert choose([ref("a", formats=f, date="2001-05"), ref("b", formats=f, date="2001")]) == "b"
    assert (
        choose([ref("a", formats=f, date="2001-05-02"), ref("b", formats=f, date="2001-05-01")])
        == "b"
    )
    assert choose([ref("a", formats=f), ref("b", formats=f, date="2030")]) == "b"


def test_most_tracks_then_country_then_status_then_id() -> None:
    f, d = ("CD",), "2000"
    assert (
        choose(
            [
                ref("a", formats=f, date=d, track_count=10),
                ref("b", formats=f, date=d, track_count=12),
            ]
        )
        == "b"
    )
    both = {"formats": f, "date": d, "track_count": 10}
    assert choose([ref("a", country="US", **both), ref("b", country="XW", **both)]) == "b"
    assert (
        choose([ref("a", country="US", **both), ref("b", country="GB", **both)], countries=("GB",))
        == "b"
    )
    assert (
        choose([ref("a", status="Promotion", **both), ref("b", status="Official", **both)]) == "b"
    )
    assert choose([ref("b", **both), ref("a", **both)]) == "a"


def test_owned_tracks_beat_media_and_date() -> None:
    a = ref("a", formats=("Vinyl",), date="2010")
    b = ref("b", formats=("CD",), date="2000")
    assert choose([a, b]) == "b"
    assert choose([a, b], owned={"a": 14, "b": 1}) == "a"


def test_official_beats_earlier_promotion() -> None:
    f = ("CD",)
    promo = ref("a", formats=f, date="2000", status="Promotion")
    retail = ref("b", formats=f, date="2005", status="Official")
    assert choose([promo, retail]) == "b"
    assert choose([promo, retail], owned={"x": 3}) == "b"  # owned tracks on no listed release


def test_media_still_ranks_ahead_of_official() -> None:
    vinyl = ref("a", formats=("Vinyl",), status="Official", date="2000")
    promo_cd = ref("b", formats=("CD",), status="Promotion", date="2001")
    assert choose([vinyl, promo_cd], media=("CD",)) == "b"


def test_owned_tracks_beat_official_status() -> None:
    f = ("CD",)
    promo = ref("a", formats=f, status="Promotion")
    official = ref("b", formats=f, status="Official")
    assert choose([promo, official], owned={"a": 3, "b": 1}) == "a"


def test_equal_owned_counts_fall_back_to_metadata_then_id() -> None:
    f = ("CD",)
    owned = {"a": 2, "b": 2}
    early = ref("b", formats=f, date="2000")
    late = ref("a", formats=f, date="2001")
    assert choose([late, early], owned=owned) == "b"
    assert choose([ref("b", formats=f), ref("a", formats=f)], owned=owned) == "a"


def test_split_maps_by_recording_and_never_twice() -> None:
    canon = [
        TrackInfo("One", recording_id="r1", index=1),
        TrackInfo("One again", recording_id="r1", index=2),
    ]
    files = [
        (Path(n), TrackInfo(n, recording_id=r), None)
        for n, r in (("x", "r1"), ("y", "r1"), ("z", "r1"), ("w", "r9"), ("v", None))
    ]
    mapped, bonus = split(files, canon)
    assert [(p.name, t.index) for p, t, _ in mapped] == [("x", 1), ("y", 2)]
    assert [p.name for p, _, _ in bonus] == ["z", "w", "v"]
