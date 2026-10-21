import pytest

from mixsync.core.matching import Band, FileTrack, Profile, TrackInfo
from mixsync.core.types import AcoustIdResult
from mixsync.match.profiles import BALANCED, LOOSE, STRICT, band
from mixsync.match.vetoes import all_confident, find_vetoes


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


def test_all_confident_needs_acoustid_to_name_each_paired_recording() -> None:
    t = TrackInfo(title="t", recording_id="rec-1")

    def file(*results: AcoustIdResult) -> FileTrack:
        return FileTrack("t", "a", acoustid=results)

    assert all_confident([(file(AcoustIdResult("x", 0.95, ("rec-1",))), t)])
    assert not all_confident([])
    assert not all_confident([(file(), t)])  # no AcoustID result
    assert not all_confident([(file(AcoustIdResult("x", 0.95, ())), t)])  # sure but names nothing
    assert not all_confident([(file(AcoustIdResult("x", 0.85, ("rec-1",))), t)])
    assert not all_confident([(file(AcoustIdResult("x", 0.95, ("rec-2",))), t)])
    assert not all_confident([(file(AcoustIdResult("x", 0.95, ("rec-1",))), TrackInfo(title="t"))])
    # the best result decides, not any result
    assert not all_confident(
        [(file(AcoustIdResult("x", 0.91, ("rec-1",)), AcoustIdResult("y", 0.99, ())), t)]
    )


def test_transcode_veto_only_when_measured_below_the_threshold() -> None:
    t = TrackInfo(title="Song")
    assert find_vetoes([(FileTrack("Song", "a"), t)]) == ()  # not measured
    assert find_vetoes([(FileTrack("Song", "a", high_band=0.01), t)]) == ()
    (v,) = find_vetoes([(FileTrack("Song", "a", high_band=1e-7), t)])
    assert v == "'Song': looks transcoded: almost nothing above 16 kHz"


def test_padded_preview_veto_names_where_the_silence_starts() -> None:
    t = TrackInfo(title="Song")
    f = FileTrack("Song", "a", length=240.0, trailing_silence=210.0)
    assert find_vetoes([(f, t)]) == ("'Song': silent from 0:30 to the end (4:00)",)
    fade = FileTrack("Song", "a", length=240.0, trailing_silence=3.0)
    assert find_vetoes([(fade, t)]) == ()
    assert find_vetoes([(FileTrack("Song", "a", length=240.0), t)]) == ()
