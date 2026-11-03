"""Deleting library music to the trash through a delete plan, end to end: the routes against a
real Merger, jobs run by the worker's run_job. Lives here for the `world` fixture."""

import re
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from mixsync.acquire.merge import Merger
from mixsync.acquire.requests import create_request
from mixsync.core.jobs import JobKind
from mixsync.db.auth import create_user
from mixsync.db.models.library import Plan, Track
from mixsync.db.models.safety import FileOp
from mixsync.db.models.work import Job
from mixsync.db.models.work import Request as AlbumRequest
from mixsync.library import fragmentation, tags

from .test_merge import GROUP, OLD, World, build, world  # noqa: F401  # world is a fixture
from .test_merge_web import _client, _drain

BUTTON = "Delete request and its music"


def _login(c: TestClient, user: str) -> str:
    c.cookies.clear()
    c.post("/login", data={"username": user, "password": "pw"})
    m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', c.get("/").text)
    assert m
    return m.group(1)


def _setup(w: World) -> TestClient:
    """root (admin), alice and bob (plain users). Alice owns request 1 (OLD r1, r2) and request
    2 (OLD r3, r9); the canonical r3 belongs to no request."""
    c = _client(w)
    # the unverified list reads tags from the app's library: make it the test's
    c.app.state.services = replace(c.app.state.services, fileops=w.ops)  # type: ignore[attr-defined]
    with w.sessions.begin() as s:
        alice = create_user(s, "alice", "pw")
        create_user(s, "bob", "pw")
        for n, recs in enumerate((("r1", "r2"), ("r3", "r9")), 1):
            req = create_request(
                s, datetime.now(UTC), alice.id, f"rel-{n}", release_group_mbid=GROUP
            )
            req.status = "imported"
            s.flush()
            for job in s.scalars(select(Job)):  # the search job a new request queues
                s.delete(job)
            for rec in recs:
                s.execute(
                    update(Track)
                    .where(Track.recording_mbid == rec, Track.release_mbid == OLD)
                    .values(batch_id=f"request-{req.id}-decision-1")
                )
    return c


def _paths(w: World, *where: Any) -> list[str]:
    with w.sessions() as s:
        return sorted(s.scalars(select(Track.path).where(*where)))


def _ids(w: World, *where: Any) -> list[int]:
    with w.sessions() as s:
        return list(s.scalars(select(Track.id).where(*where)))


def _rows(w: World) -> list[dict[str, Any]]:
    with w.sessions() as s:
        return [fragmentation.row_dict(t) for t in s.scalars(select(Track).order_by(Track.id))]


def _request_ids(w: World) -> list[int]:
    with w.sessions() as s:
        return list(s.scalars(select(AlbumRequest.id).order_by(AlbumRequest.id)))


def _plan_ids(w: World) -> list[int]:
    with w.sessions() as s:
        return list(s.scalars(select(Plan.id)))


def _rescans(w: World) -> int:
    with w.sessions() as s:
        return len(list(s.scalars(select(Job).where(Job.kind == JobKind.RESCAN))))


def _post(c: TestClient, tok: str, url: str, **data: str) -> int:
    return c.post(url, data={"csrf_token": tok, **data}).status_code


def _preview(c: TestClient, tok: str, url: str, **data: str) -> int:
    r = c.post(url, data={"csrf_token": tok, **data})
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


def _trashed(w: World, pid: int) -> list[FileOp]:
    with w.sessions() as s:
        return list(
            s.scalars(
                select(FileOp).where(FileOp.batch_id == f"delete-{pid}", FileOp.kind == "trash")
            )
        )


def _roundtrip(w: World, c: TestClient, tok: str, url: str, expect: list[str], **form: str) -> None:
    """Preview, confirm, run, undo, run, as one user."""
    before_rows, before_lib = _rows(w), w.lib()
    pid = _preview(c, tok, url, **form)
    page = c.get(f"/library/plans/{pid}").text
    plan = w.plan(pid)
    assert plan.kind == "delete" and plan.status == "draft"
    assert sorted(op["old"]["path"] for op in plan.ops if op["op"] == "trash") == expect
    assert all(p in page for p in expect)
    assert w.lib() == before_lib and _rows(w) == before_rows  # a dry run changes nothing
    assert _post(c, tok, f"/library/plans/{pid}/confirm") == 303
    _drain(w)
    assert w.plan(pid).status == "done", w.plan(pid).error
    assert set(w.lib()) == set(before_lib) - set(expect)
    assert [r["path"] for r in _rows(w)] == [
        r["path"] for r in before_rows if r["path"] not in expect
    ]
    done = _trashed(w, pid)
    assert sorted(o.src for o in done) == expect and {o.status for o in done} == {"done"}
    assert _rescans(w) >= 1
    assert _post(c, tok, f"/library/plans/{pid}/undo") == 303
    _drain(w)
    assert w.plan(pid).status == "undone", w.plan(pid).error
    assert w.lib() == before_lib
    assert _rows(w) == before_rows  # every field, id and import time included


def test_owner_deletes_request_and_its_music_then_undoes(world: World) -> None:  # noqa: F811
    c = _setup(world)
    mine = _paths(world, Track.batch_id.like("request-1-%"))
    other = _paths(world, Track.batch_id.like("request-2-%"))
    assert len(mine) == 2 and len(other) == 2
    with c:
        tok = _login(c, "alice")
        assert BUTTON in c.get("/requests").text
        before_rows, before_lib = _rows(world), world.lib()
        pid = _preview(c, tok, "/requests/1/delete-with-music")
        page = c.get(f"/library/plans/{pid}")
        assert page.status_code == 200 and all(p in page.text for p in mine)
        assert not any(p in page.text for p in other)
        assert _request_ids(world) == [1, 2]  # the dry run deletes nothing
        assert _post(c, tok, f"/library/plans/{pid}/confirm") == 303
        _drain(world)
        assert world.plan(pid).status == "done", world.plan(pid).error
        assert _request_ids(world) == [2]
        assert _paths(world, Track.batch_id.like("request-1-%")) == []
        assert _paths(world, Track.batch_id.like("request-2-%")) == other  # untouched
        assert set(world.lib()) == set(before_lib) - set(mine)
        assert sorted(o.src for o in _trashed(world, pid)) == mine
        assert _rescans(world) >= 1
        assert _post(c, tok, f"/library/plans/{pid}/undo") == 303
        _drain(world)
        assert world.plan(pid).status == "undone", world.plan(pid).error
        assert world.lib() == before_lib and _rows(world) == before_rows
        assert _request_ids(world) == [2]  # the request is not recreated


def test_unverified_album_group_roundtrip(world: World) -> None:  # noqa: F811
    c = _setup(world)
    with world.sessions.begin() as s:
        s.execute(update(Track).where(Track.release_mbid == OLD).values(status="unverified"))
    old_files = _paths(world, Track.release_mbid == OLD)
    with c:
        tok = _login(c, "root")
        shown = c.get("/library/unverified").text
        assert all(p in shown for p in old_files)
        form = {"albumartist": "Band", "album": "Album", "year": "2000"}
        _roundtrip(world, c, tok, "/library/unverified/delete", old_files, **form)


def test_unverified_group_without_album_tag_deletes_only_that_group(world: World) -> None:  # noqa: F811
    c = _setup(world)
    with world.sessions.begin() as s:
        s.execute(update(Track).where(Track.release_mbid == OLD).values(status="unverified"))
    old_files = _paths(world, Track.release_mbid == OLD)
    bare, others = old_files[0], old_files[1:]
    tags.write(world.ops.library / bare, tags.TagSet("Bare", "", "", "", 1, 1))
    with c:
        tok = _login(c, "root")
        page = c.get("/library/unverified").text
        assert page.count("Delete this album from the library") == 2  # tagged group and bare one
        assert page.count("Submit to MusicBrainz") == 1  # the album-tag-only actions stay
        assert page.count("Check MusicBrainz now") == 1
        _roundtrip(
            world, c, tok, "/library/unverified/delete", [bare], albumartist="", album="", year=""
        )
        assert _paths(world, Track.release_mbid == OLD) == sorted(old_files)
    assert others  # the tagged group was never part of the plan


def test_two_untagged_groups_delete_independently(world: World) -> None:  # noqa: F811
    c = _setup(world)
    with world.sessions.begin() as s:
        s.execute(update(Track).where(Track.release_mbid == OLD).values(status="unverified"))
    a, b, *others = _paths(world, Track.release_mbid == OLD)
    tags.write(world.ops.library / a, tags.TagSet("A", "", "", "Artist A", 1, 1, year=1999))
    tags.write(world.ops.library / b, tags.TagSet("B", "", "", "Artist B", 1, 1, year=2005))
    with c:
        tok = _login(c, "root")
        page = c.get("/library/unverified").text
        assert page.count("Delete this album from the library") == 3  # tagged group and both bare
        form = {"album": "", "year": "1999"}
        pid = _preview(c, tok, "/library/unverified/delete", albumartist="Artist A", **form)
        assert [o["old"]["path"] for o in world.plan(pid).ops if o["op"] == "trash"] == [a]
        _roundtrip(
            world,
            c,
            tok,
            "/library/unverified/delete",
            [b],
            albumartist="Artist B",
            album="",
            year="2005",
        )
        # both other groups are still listed after the delete round trip
        assert c.get("/library/unverified").text.count("Delete this album from the library") == 3
    assert others and b in _paths(world, Track.release_mbid == OLD)


def test_single_track_roundtrip(world: World) -> None:  # noqa: F811
    c = _setup(world)
    (tid,) = _ids(world, Track.release_mbid != OLD)
    (path,) = _paths(world, Track.id == tid)
    with c:
        _roundtrip(world, c, _login(c, "root"), f"/library/tracks/{tid}/delete", [path])


def test_permissions(world: World) -> None:  # noqa: F811
    c = _setup(world)
    (tid,) = _ids(world, Track.release_mbid != OLD)
    with c:
        bob = _login(c, "bob")  # neither the owner nor library.edit
        assert _post(c, bob, "/requests/1/delete-with-music") == 403
        alice = _login(c, "alice")  # owner of the request, not of the library
        assert _post(c, alice, f"/library/tracks/{tid}/delete") == 403
        assert _post(c, alice, "/library/unverified/delete", album="Album") == 403
        assert _plan_ids(world) == []
        # library.edit may use the request-page action on someone else's request
        pid = _preview(c, _login(c, "root"), "/requests/1/delete-with-music")
        bob = _login(c, "bob")
        assert c.get(f"/library/plans/{pid}").status_code == 403
        for act in ("confirm", "discard", "undo", "resume"):
            assert _post(c, bob, f"/library/plans/{pid}/{act}") == 403
        # the owner may open, confirm, discard, undo and resume it (a draft cannot resume: 409)
        alice = _login(c, "alice")
        assert c.get(f"/library/plans/{pid}").status_code == 200
        assert _post(c, alice, f"/library/plans/{pid}/resume") == 409
        assert _post(c, alice, f"/library/plans/{pid}/discard") == 303
        assert world.plan(pid).status == "discarded"
        # and nothing else: a track-delete plan of root's is closed to her
        root = _login(c, "root")
        tpid = _preview(c, root, f"/library/tracks/{tid}/delete")
        alice = _login(c, "alice")
        assert c.get(f"/library/plans/{tpid}").status_code == 403
        assert _post(c, alice, f"/library/plans/{tpid}/confirm") == 403
        assert world.plan(tpid).status == "draft" and _request_ids(world) == [1, 2]


def test_owner_undoes_her_own_plan(world: World) -> None:  # noqa: F811
    c = _setup(world)
    with c:
        alice = _login(c, "alice")
        pid = _preview(c, alice, "/requests/1/delete-with-music")
        assert _post(c, alice, f"/library/plans/{pid}/confirm") == 303
        _drain(world)
        assert _post(c, _login(c, "bob"), f"/library/plans/{pid}/undo") == 403
        assert _post(c, _login(c, "alice"), f"/library/plans/{pid}/undo") == 303


def test_request_page_offers_the_action_only_with_music(world: World) -> None:  # noqa: F811
    c = _setup(world)
    with world.sessions.begin() as s:
        for t in s.scalars(select(Track).where(Track.batch_id.like("request-2-%"))):
            s.delete(t)
    with c:
        _login(c, "alice")
        page = c.get("/requests").text
        assert page.count(BUTTON) == 1 and "/requests/1/delete-with-music" in page


def test_undo_refused_after_the_window_and_after_a_purge(
    world: World,  # noqa: F811
    tmp_path: Path,
) -> None:
    c = _setup(world)
    first, second = _ids(world, Track.release_mbid == OLD)[:2]
    with c:
        tok = _login(c, "root")
        pids: list[int] = []
        for t in (first, second):
            pids.append(_preview(c, tok, f"/library/tracks/{t}/delete"))
            assert _post(c, tok, f"/library/plans/{pids[-1]}/confirm") == 303
            _drain(world)
            assert world.plan(pids[-1]).status == "done"
        rows, lib = _rows(world), world.lib()
        with world.sessions.begin() as s:  # finished 31 days ago
            s.get_one(Plan, pids[0]).updated_at = datetime.now(UTC) - timedelta(days=31)
        assert _post(c, tok, f"/library/plans/{pids[0]}/undo") == 409
        assert world.plan(pids[0]).status == "done"
        # inside the window, but the trash is gone: the undo fails and restores nothing
        (op,) = _trashed(world, pids[1])
        shutil.move(world.ops.trash_dir / op.dst, tmp_path / "purged.flac")
        assert _post(c, tok, f"/library/plans/{pids[1]}/undo") == 303
        _drain(world)
        plan = world.plan(pids[1])
        assert plan.status == "failed" and "is gone" in (plan.error or "")
        assert _rows(world) == rows and world.lib() == lib


def test_confirm_above_fifty_files_needs_the_typed_count(world: World) -> None:  # noqa: F811
    c = _setup(world)
    with world.sessions.begin() as s:
        for n in range(51):
            (world.ops.library / f"Bulk/{n}.flac").parent.mkdir(exist_ok=True)
            (world.ops.library / f"Bulk/{n}.flac").write_bytes(b"x")
            s.add(
                Track(
                    path=f"Bulk/{n}.flac",
                    status="unverified",
                    batch_id="bulk",
                    imported_at=datetime.now(UTC),
                )
            )
    ids = _ids(world, Track.batch_id == "bulk")
    with c:
        tok = _login(c, "root")
        # no tags to group them by, so build through the service: one plan of 51 files
        pid = world.merger.build_delete(ids)
        confirm = f"/library/plans/{pid}/confirm"
        assert "Type 51 to confirm" in c.get(f"/library/plans/{pid}").text
        assert _post(c, tok, confirm) == 400
        assert _post(c, tok, confirm, count="50") == 400
        assert not world.trashed()
        assert _post(c, tok, confirm, count="51") == 303
        _drain(world)
        assert world.plan(pid).status == "done" and len(_trashed(world, pid)) == 51


def test_refused_while_another_plan_blocks_the_group(world: World) -> None:  # noqa: F811
    c = _setup(world)
    (tid,) = _ids(world, Track.release_mbid != OLD)
    other = _ids(world, Track.release_mbid == OLD)[0]
    with c:
        tok = _login(c, "root")
        for status in ("running", "undoing", "failed"):
            merge = build(world)
            with world.sessions.begin() as s:
                s.get_one(Plan, merge).status = status
            assert _post(c, tok, f"/library/tracks/{other}/delete") == 409
            assert _post(c, _login(c, "alice"), "/requests/1/delete-with-music") == 409
            with world.sessions.begin() as s:
                s.get_one(Plan, merge).status = "discarded"
            tok = _login(c, "root")
        # the canonical track is in another release of the same group
        assert len(_plan_ids(world)) == 3  # the three merge previews, and no delete plan
        assert _preview(c, tok, f"/library/tracks/{tid}/delete")  # no plan blocks now
        assert not world.trashed()


def test_running_delete_blocks_a_merge_and_a_second_delete(world: World) -> None:  # noqa: F811
    from mixsync.acquire.merge import PlanInProgress

    c = _setup(world)
    first, second = _ids(world, Track.release_mbid == OLD)[:2]
    with c:
        tok = _login(c, "root")
        pid = _preview(c, tok, f"/library/tracks/{first}/delete")
        assert _post(c, tok, f"/library/plans/{pid}/confirm") == 303  # confirmed, not yet run
        with pytest.raises(PlanInProgress):
            build(world)
        assert _post(c, tok, f"/library/tracks/{first}/delete") == 409
        assert _post(c, tok, f"/library/tracks/{second}/delete") == 409  # same release group
        _drain(world)
        assert world.plan(pid).status == "done"


@pytest.mark.parametrize("status", ["downloading", "verifying", "importing"])
def test_refused_while_a_request_for_the_group_is_busy(world: World, status: str) -> None:  # noqa: F811
    c = _setup(world)
    with world.sessions.begin() as s:
        s.get_one(AlbumRequest, 2).status = status
    with c:
        tok = _login(c, "root")
        assert _post(c, tok, f"/library/tracks/{_ids(world)[0]}/delete") == 409
        assert _post(c, tok, "/requests/1/delete-with-music") == 409
        assert _plan_ids(world) == []


@pytest.mark.parametrize("where", ["../outside.flac", "../discover/x.flac", "ABSOLUTE"])
def test_path_outside_the_library_is_refused(world: World, tmp_path: Path, where: str) -> None:  # noqa: F811
    c = _setup(world)
    (world.ops.data_dir / "discover").mkdir(exist_ok=True)
    (world.ops.data_dir / "outside.flac").write_bytes(b"keep")
    (world.ops.data_dir / "discover" / "x.flac").write_bytes(b"keep")
    path = str(world.ops.data_dir / "outside.flac") if where == "ABSOLUTE" else where
    with world.sessions.begin() as s:
        t = Track(path=path, status="unverified", batch_id="odd", imported_at=datetime.now(UTC))
        s.add(t)
        s.flush()
        tid = t.id
    before = world.lib()
    with c:
        tok = _login(c, "root")
        assert _post(c, tok, f"/library/tracks/{tid}/delete") == 409
    assert _plan_ids(world) == [] and world.lib() == before and not world.trashed()
    assert (world.ops.data_dir / "outside.flac").read_bytes() == b"keep"
    assert (world.ops.data_dir / "discover" / "x.flac").read_bytes() == b"keep"


def test_merge_trash_op_without_keeper_trashes_nothing(world: World) -> None:  # noqa: F811
    pid = build(world)
    with world.sessions.begin() as s:
        plan = s.get_one(Plan, pid)
        plan.ops = [
            {k: v for k, v in op.items() if k != "keeper"} for op in plan.ops if op["op"] == "trash"
        ]
    with pytest.raises(KeyError):
        world.merger.execute(pid)
    assert world.trashed() == [] and world.plan(pid).status == "failed"


def _age(w: World, pid: int, days: int) -> None:
    with w.sessions.begin() as s:
        s.get_one(Plan, pid).created_at = datetime.now(UTC) - timedelta(days=days)


def test_stale_delete_draft_is_discarded_by_a_new_preview(world: World) -> None:  # noqa: F811
    c = _setup(world)
    first, second = _ids(world, Track.release_mbid == OLD)[:2]
    with c:
        tok = _login(c, "root")
        old = _preview(c, tok, f"/library/tracks/{first}/delete")
        fresh = _preview(c, tok, f"/library/tracks/{second}/delete")
        merge = build(world)
        _age(world, old, 8)
        _age(world, merge, 30)
        _age(world, fresh, 6)
        _preview(c, tok, f"/library/tracks/{second}/delete")
        assert world.plan(old).status == "discarded"
        assert world.plan(fresh).status == "draft"
        assert world.plan(merge).status == "draft"  # merge drafts are not swept


def test_stale_delete_draft_cannot_be_confirmed(world: World) -> None:  # noqa: F811
    c = _setup(world)
    (tid,) = _ids(world, Track.release_mbid != OLD)
    before, rows = world.lib(), _rows(world)
    with c:
        tok = _login(c, "root")
        pid = _preview(c, tok, f"/library/tracks/{tid}/delete")
        _age(world, pid, 8)
        assert _post(c, tok, f"/library/plans/{pid}/confirm") == 409
        assert world.plan(pid).status == "discarded"
        _drain(world)
        assert world.lib() == before and _rows(world) == rows and not world.trashed()


def test_six_day_old_delete_draft_still_confirms(world: World) -> None:  # noqa: F811
    c = _setup(world)
    (tid,) = _ids(world, Track.release_mbid != OLD)
    with c:
        tok = _login(c, "root")
        pid = _preview(c, tok, f"/library/tracks/{tid}/delete")
        _age(world, pid, 6)
        assert _post(c, tok, f"/library/plans/{pid}/confirm") == 303
        _drain(world)
        assert world.plan(pid).status == "done"


def _fail_first_trash(monkeypatch: pytest.MonkeyPatch) -> None:
    real, calls = Merger._trash, []  # pyright: ignore[reportPrivateUsage]

    def flaky(self: Merger, *a: Any, **k: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        real(self, *a, **k)

    monkeypatch.setattr(Merger, "_trash", flaky)


def test_a_failed_delete_keeps_the_request_and_the_owner_resumes_it(
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _setup(world)
    mine = _paths(world, Track.batch_id.like("request-1-%"))
    _fail_first_trash(monkeypatch)
    with c:
        alice = _login(c, "alice")
        pid = _preview(c, alice, "/requests/1/delete-with-music")
        assert _post(c, alice, f"/library/plans/{pid}/confirm") == 303
        assert _request_ids(world) == [1, 2]  # confirm deletes nothing
        _drain(world)
        assert world.plan(pid).status == "failed"
        assert _request_ids(world) == [1, 2]  # the request outlives the failed run
        assert set(mine) <= set(world.lib())
        assert f"/library/plans/{pid}" in c.get("/requests").text  # she can find the plan
        assert _post(c, _login(c, "bob"), f"/library/plans/{pid}/resume") == 403
        assert (
            _post(c, _login(c, "alice"), f"/library/plans/{pid}/resume") == 303
        )  # no library.edit
        _drain(world)
        assert world.plan(pid).status == "done", world.plan(pid).error
        assert _request_ids(world) == [2]
        assert not set(mine) & set(world.lib())
        assert f"/library/plans/{pid}" not in c.get("/requests").text


def test_requests_page_links_a_confirmed_delete_plan_until_it_is_done(world: World) -> None:  # noqa: F811
    c = _setup(world)
    with c:
        alice = _login(c, "alice")
        pid = _preview(c, alice, "/requests/1/delete-with-music")
        assert f"/library/plans/{pid}" not in c.get("/requests").text  # a preview is not running
        assert _post(c, alice, f"/library/plans/{pid}/confirm") == 303
        assert f"/library/plans/{pid}" in c.get("/requests").text  # queued
        _drain(world)
        assert f"/library/plans/{pid}" not in c.get("/requests").text


def test_confirm_refused_changes_nothing(world: World) -> None:  # noqa: F811
    c = _setup(world)
    with c:
        alice = _login(c, "alice")
        pid = _preview(c, alice, "/requests/1/delete-with-music")
        with world.sessions.begin() as s:
            s.get_one(AlbumRequest, 1).status = "downloading"
        before = world.lib()
        assert _post(c, alice, f"/library/plans/{pid}/confirm") == 409
        _drain(world)
        assert world.plan(pid).status == "draft" and not world.trashed()
        assert world.lib() == before and _request_ids(world) == [1, 2]


def test_late_refusal_completes_the_plan_and_keeps_the_request(world: World) -> None:  # noqa: F811
    c = _setup(world)
    mine = _paths(world, Track.batch_id.like("request-1-%"))
    with c:
        alice = _login(c, "alice")
        pid = _preview(c, alice, "/requests/1/delete-with-music")
        assert _post(c, alice, f"/library/plans/{pid}/confirm") == 303
        with world.sessions.begin() as s:  # the request changed after confirm
            s.get_one(AlbumRequest, 1).status = "downloading"
        _drain(world)
        plan = world.plan(pid)
        assert plan.status == "done" and "request is in progress" in (plan.error or "")
        assert _request_ids(world) == [1, 2]
        assert not set(mine) & set(world.lib())  # the files stay trashed
        page = c.get(f"/library/plans/{pid}").text
        assert "Request kept: request is in progress" in page
