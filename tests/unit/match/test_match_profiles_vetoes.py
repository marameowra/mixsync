import pytest

from mixsync.core.matching import Band, FileTrack, Profile, TrackInfo
from mixsync.match.profiles import BALANCED, LOOSE, STRICT, band
from mixsync.match.vetoes import find_vetoes


@pytest.mark.parametrize(
    ("profile", "auto", "review"),
    [(STRICT, 0.04, 0.30), (BALANCED, 0.08, 0.40), (LOOSE, 0.15, 0.55)],
)
def test_band_edges(profile: Profile, auto: float, review: float) -> None:
    assert band(0.0, (), profile) == Band.AUTO_ACCEPT
    assert band(auto, (), profile) == Band.AUTO_ACCEPT
    assert band(auto + 1e-9, (), profile) == Band.REVIEW
    assert band(review, (), profile) == Band.REVIEW
    assert band(review + 1e-9, (), profile) == Band.REJECT


def test_veto_forces_review_at_any_distance() -> None:
    assert band(0.0, ["v"], BALANCED) == Band.REVIEW
    assert band(0.9, ["v"], BALANCED) == Band.REVIEW


def test_duration_veto() -> None:
    t = TrackInfo(title="t", length=200.0)
    assert find_vetoes([(FileTrack("t", "a", length=215.0), t)]) == ()
    assert find_vetoes([(FileTrack("t", "a", length=185.0), t)]) == ()
    (v,) = find_vetoes([(FileTrack("t", "a", length=215.5), t)])
    assert "duration" in v
    assert find_vetoes([(FileTrack("t", "a"), t)]) == ()
    assert find_vetoes([(FileTrack("t", "a", length=1.0), TrackInfo(title="t"))]) == ()


def test_recording_id_veto() -> None:
    t = TrackInfo(title="t", recording_id="rec-1")
    assert find_vetoes([(FileTrack("t", "a", recording_id="rec-1"), t)]) == ()
    assert find_vetoes([(FileTrack("t", "a"), t)]) == ()
    (v,) = find_vetoes([(FileTrack("t", "a", recording_id="rec-2"), t)])
    assert "rec-2" in v
