from typing import get_args

from mixsync.acquire import lifecycle
from mixsync.db.models.work import Request, Status


def _req(status: Status = "searching", **kw: object) -> Request:
    return Request(user_id=1, release_mbid="m", status=status, recording_mbids=["a", "b"], **kw)


def test_a_partial_import_keeps_looking_and_a_failed_download_keeps_the_note() -> None:
    r = _req()
    lifecycle.keep_looking(r, "2 of 5 tracks present", ["1 file(s) not on the release: x"])
    assert r.status == "partial" and r.partial_note == "2 of 5 tracks present"
    assert r.last_error == "partial: 2 of 5 tracks present; 1 file(s) not on the release: x"
    lifecycle.download_failed(r, True, "rejected after download")
    assert r.status == "partial"
    assert r.last_error == "partial: 2 of 5 tracks present; rejected after download"


def test_download_failed_without_tracks_searches_again_and_from_files_fails() -> None:
    r = _req()
    lifecycle.download_failed(r, False, "boom")
    assert (r.status, r.last_error) == ("searching", "boom")
    f = _req(source="drop")
    lifecycle.download_failed(f, True, "boom")
    assert (f.status, f.last_error) == ("failed", "boom")


def test_no_results_keeps_a_partial_label() -> None:
    p, s = _req("partial"), _req("searching")
    lifecycle.no_results(p)
    lifecycle.no_results(s)
    assert (p.status, s.status) == ("partial", "no_results")


def test_dead_ends_and_the_recordings_they_rule_out() -> None:
    whole = _req()
    lifecycle.dead_end(whole, "none of the wanted recordings are on this release")
    assert lifecycle.is_dead_end(whole) and lifecycle.dead_recordings(whole, {"a"}) == ["a", "b"]
    some = _req()
    lifecycle.finished(some, [], ["1 of 2 wanted recordings not on this release"], dead_end=True)
    assert lifecycle.is_dead_end(some) and lifecycle.dead_recordings(some, {"a"}) == ["b"]
    clean, broken = _req(), _req()
    lifecycle.finished(clean, [], [])
    lifecycle.finished(broken, ["x: bad"], [], dead_end=True)
    assert not lifecycle.is_dead_end(clean) and not lifecycle.is_dead_end(broken)
    assert (clean.status, broken.status) == ("imported", "failed")


def test_status_questions() -> None:
    assert lifecycle.can_search(_req("no_results")) and not lifecycle.can_search(_req("review"))
    assert not lifecycle.can_search(_req("partial", source="adopt"))
    assert lifecycle.is_busy(_req("importing")) and lifecycle.is_waiting(_req("partial"))


def test_a_job_failing_for_good_fails_the_request_unless_tracks_are_in() -> None:
    r = _req(dead_end=True)
    lifecycle.job_failed(r, False, "boom")
    assert (r.status, r.last_error, r.dead_end) == ("failed", "boom", False)
    p = _req("partial", dead_end=True)
    lifecycle.keep_looking(p, "2 of 5 tracks present")
    lifecycle.job_failed(p, True, "boom")
    assert p.status == "partial" and not p.dead_end
    assert p.last_error == "partial: 2 of 5 tracks present; boom"


def test_every_status_is_in_a_set_and_unverified_is_done_but_has_a_file() -> None:
    sets = (lifecycle.OPEN, lifecycle.DONE, lifecycle.IN_PROGRESS)
    assert all(any(st in group for group in sets) for st in get_args(Status))
    assert "unverified" in lifecycle.DONE and "unverified" not in lifecycle.NO_FILE


def test_only_dead_end_transitions_set_dead_end() -> None:
    to_clear = (
        lambda r: lifecycle.failed(r, "x"),
        lifecycle.owned,
        lifecycle.no_results,
        lifecycle.downloading,
        lifecycle.verifying,
        lifecycle.importing,
        lifecycle.review,
        lifecycle.rejected,
        lambda r: lifecycle.download_failed(r, True, "x"),
        lambda r: lifecycle.job_failed(r, True, "x"),
        lambda r: lifecycle.download_failed(r, False, "x"),
        lambda r: lifecycle.job_failed(r, False, "x"),
        lambda r: lifecycle.keep_looking(r, "1 of 2 tracks present"),
        lambda r: lifecycle.finished(r, [], [], dead_end=False),
        lambda r: lifecycle.finished(r, ["boom"], [], dead_end=True),
    )
    for transition in to_clear:
        r = _req()
        lifecycle.dead_end(r, "x")
        assert r.status == "failed" and r.dead_end
        transition(r)
        assert not r.dead_end
    r = _req()
    lifecycle.finished(r, [], ["x"], dead_end=True)
    assert r.dead_end


def test_only_keep_looking_sets_the_partial_note_and_in_flight_transitions_keep_it() -> None:
    keep = (
        lifecycle.no_results,
        lifecycle.downloading,  # the next candidate of a partial request
        lifecycle.verifying,
        lifecycle.importing,
        lambda r: lifecycle.download_failed(r, True, "x"),
        lambda r: lifecycle.job_failed(r, True, "x"),
    )
    clear = (
        lambda r: lifecycle.failed(r, "x"),
        lifecycle.owned,
        lifecycle.review,
        lifecycle.rejected,
        lambda r: lifecycle.dead_end(r, "x"),
        lambda r: lifecycle.download_failed(r, False, "x"),
        lambda r: lifecycle.job_failed(r, False, "x"),
        lambda r: lifecycle.finished(r, [], []),
    )
    for transition, kept in [(t, True) for t in keep] + [(t, False) for t in clear]:
        r = _req()
        lifecycle.keep_looking(r, "1 of 2 tracks present")
        transition(r)
        assert (r.partial_note == "1 of 2 tracks present") is kept
