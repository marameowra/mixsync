from mixsync.acquire import lifecycle
from mixsync.db.models.work import Request


def _req(status: str = "searching", **kw: object) -> Request:
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
