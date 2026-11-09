import pytest

from mixsync.core.matching import Band, FileTrack, Profile, TrackInfo
from mixsync.core.types import AcoustIdResult, NamedRecording
from mixsync.match.profiles import BALANCED, LOOSE, STRICT, band
from mixsync.match.scorer import assign_tracks
from mixsync.match.vetoes import MAX_DURATION_DELTA, all_confident, find_vetoes


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


def _dup(*recs: NamedRecording, score: float = 0.97) -> tuple[AcoustIdResult, ...]:
    return (AcoustIdResult("x", score, tuple(r.id for r in recs), recs),)


ROSES = TrackInfo(title="Roses (Imanbek remix)", length=176.0, recording_id="240c")


def test_duplicate_recording_with_same_title_and_length_is_the_tracks_recording() -> None:
    # review 2484: AcoustID names two unmerged duplicates, not the release track's id
    acoustid = _dup(
        NamedRecording("8854", "Roses (Imanbek remix)", 176.0),
        NamedRecording("ae8d", "Roses (Imanbek remix)", 176.133),
        NamedRecording("018e", "Roses", 173.0),
    )
    f = FileTrack("Roses (Imanbek Remix)", "SAINt JHN", length=176.0, acoustid=acoustid)
    pairs, extra, missing = assign_tracks([f], [ROSES])
    assert pairs == [(f, ROSES)] and not extra and not missing
    assert find_vetoes(pairs, extra, [ROSES]) == ()
    assert all_confident(pairs)


@pytest.mark.parametrize(
    ("title", "duration"),
    [("Heck", 176.0), ("Roses (Imanbek remix)", 176.0 + MAX_DURATION_DELTA + 1)],
)
def test_duplicate_recording_with_other_title_or_length_still_vetoes(
    title: str, duration: float
) -> None:
    f = FileTrack("x", "a", length=176.0, acoustid=_dup(NamedRecording("8854", title, duration)))
    assert not all_confident([(f, ROSES)])
    (v,) = find_vetoes([(f, ROSES)])
    assert "different recording" in v
    (w,) = find_vetoes([], [f], [ROSES])
    assert "does not contain" in w
    assert assign_tracks([f], [ROSES])[0] == []


def test_acoustid_without_title_or_duration_matches_by_id_only() -> None:
    f = FileTrack("Roses (Imanbek remix)", "a", length=176.0, acoustid=_dup(NamedRecording("8854")))
    assert not all_confident([(f, ROSES)])
    assert find_vetoes([(f, ROSES)])
    old = FileTrack("t", "a", length=176.0, acoustid=(AcoustIdResult("x", 0.97, ("8854",)),))
    assert find_vetoes([(old, ROSES)])
