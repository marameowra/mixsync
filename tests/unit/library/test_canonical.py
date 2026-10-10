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
