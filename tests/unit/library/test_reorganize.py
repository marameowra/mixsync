"""Reorganize after a path template change (#285, ADR 0019): a dry-run plan of moves that keep
each file's bytes, confirmed and undone like a merge."""

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, update

from mixsync.acquire.merge import Merger
from mixsync.app import create_app
from mixsync.core.clock import SystemClock
from mixsync.core.config import Settings
from mixsync.core.errors import SafetyError
from mixsync.core.jobs import JobKind
from mixsync.db.auth import create_user
from mixsync.db.models.library import Plan, Track
from mixsync.db.models.safety import FileOp
from mixsync.db.models.work import Job
from mixsync.library import fragmentation, layout, tags
from mixsync.web.routes import library as library_routes

from . import test_merge
from .test_merge import World, _rows
from .test_merge_web import _drain, _login

world = test_merge.world  # the fixture: five tracks filed under the default template

T2 = "{albumartist}/{year} - {album}/{track:02} {title}.{ext}"
MOVES = {  # where the world's five files go under T2
    "Band/Album (1999)/01-03 Song r3.flac": "Band/1999 - Album/03 Song r3.flac",
    "Band/Album (2000)/01-01 Song r1.flac": "Band/2000 - Album/01 Song r1.flac",
    "Band/Album (2000)/01-02 Song r2.flac": "Band/2000 - Album/02 Song r2.flac",
    "Band/Album (2000)/01-03 Song r3.flac": "Band/2000 - Album/03 Song r3.flac",
    "Band/Album (2000)/01-04 Song r9.flac": "Band/2000 - Album/04 Song r9.flac",
}


def merger(w: World, template: str = T2, **kw: Any) -> Merger:
    """The worker after a restart with a changed MIXSYNC_PATH_TEMPLATE."""
    return replace(w.merger, template=template, **kw)


def rows(w: World) -> list[dict[str, Any]]:
    return [
        {**fragmentation.row_dict(t), "names": (t.album_artist, t.album, t.year)} for t in _rows(w)
    ]


def drift(w: World, template: str = T2) -> int:
    with w.sessions() as s:
        return layout.drift(s, template)


def file_tags(w: World) -> dict[str, Any]:
    return {p: tags.read_all(w.ops.library / p) for p in w.lib()}


def test_imports_record_their_template_and_a_change_moves_nothing_until_confirm(
    world: World,
) -> None:
    assert {r["path_template"] for r in rows(world)} == {world.merger.template}
    lib0, rows0 = world.lib(), rows(world)
    assert drift(world) == 5 and drift(world, world.merger.template) == 0
    pid = merger(world).build_reorganize()
    assert world.lib() == lib0 and rows(world) == rows0 and world.trashed() == []
    plan = world.plan(pid)
    assert (plan.kind, plan.status, plan.release_group_mbid) == ("reorganize", "draft", "")
    assert {o["old"]["path"]: o["new_path"] for o in plan.ops if o["op"] == "move"} == MOVES


def test_confirm_journals_each_move_keeps_the_bytes_and_undo_restores(world: World) -> None:
    lib0, rows0, tags0 = world.lib(), rows(world), file_tags(world)
    m = merger(world)
    pid = m.build_reorganize()
    m.execute(pid)
    plan = world.plan(pid)
    assert plan.status == "done", plan.error
    assert world.lib() == {new: lib0[old] for old, new in MOVES.items()}  # byte-identical
    assert file_tags(world) == {new: tags0[old] for old, new in MOVES.items()}
    with world.sessions() as s:
        journal = list(s.scalars(select(FileOp).where(FileOp.batch_id == plan.batch_id)))
    assert all(op.status == "done" for op in journal)
    assert sorted(op.src for op in journal if op.kind == "trash") == sorted(MOVES)
    assert sorted(op.dst for op in journal if op.kind == "copy") == sorted(MOVES.values())
    by_id = {r["id"]: r for r in rows0}
    for r in rows(world):
        old = by_id[r["id"]]
        assert r == {**old, "path": MOVES[old["path"]], "path_template": T2}  # names unchanged
    assert world.names_match_files() and drift(world) == 0

    m.undo_plan(pid)
    assert world.plan(pid).status == "undone", world.plan(pid).error
    assert world.lib() == lib0 and rows(world) == rows0 and file_tags(world) == tags0
    assert drift(world) == 5


def test_collisions_get_suffixes(world: World) -> None:
    m = merger(world, "{albumartist}/{album}.{ext}")
    pid = m.build_reorganize()
    news = [o["new_path"] for o in world.plan(pid).ops]
    assert news == ["Band/Album.flac", *(f"Band/Album ({n}).flac" for n in range(2, 6))]
    lib0 = world.lib()
    m.execute(pid)
    assert sorted(world.lib().values()) == sorted(lib0.values()) and sorted(world.lib()) == sorted(
        news
    )


def other_mount(p: Path) -> int:
    return 2 if "1999 - Album" in str(p) else 1


def test_a_move_onto_another_filesystem_is_refused_and_reported(world: World) -> None:
    m = merger(world, device=other_mount)
    pid = m.build_reorganize()
    refused = [o for o in world.plan(pid).ops if o["op"] == "refused"]
    assert [(o["old"]["path"], o["reason"]) for o in refused] == [
        ("Band/Album (1999)/01-03 Song r3.flac", "on another filesystem")
    ]
    m.execute(pid)
    assert "Band/Album (1999)/01-03 Song r3.flac" in world.lib()
    assert not any("1999 - Album" in p for p in world.lib())
    assert drift(world) == 1  # still reported as off-template


def test_a_mount_that_appears_after_the_build_stops_the_plan_before_moving(world: World) -> None:
    pid = merger(world).build_reorganize()
    lib0 = world.lib()
    with pytest.raises(SafetyError, match="cross filesystems"):
        merger(world, device=other_mount).execute(pid)
    assert world.lib() == lib0 and world.trashed() == []
    assert world.plan(pid).status == "failed"


def test_a_plan_is_stale_once_the_template_or_a_row_changes(world: World) -> None:
    pid = merger(world).build_reorganize()
    lib0 = world.lib()
    with pytest.raises(SafetyError, match="template changed"):
        merger(world, "{album}/{title}.{ext}").execute(pid)
    assert world.lib() == lib0
    merger(world).undo_plan(pid)  # a failed plan blocks a new one until it is undone

    pid = merger(world).build_reorganize()  # first op: the 1999 copy
    with world.sessions.begin() as s:
        s.execute(update(Track).where(Track.release_mbid == "canon").values(acoustid_id="other"))
    with pytest.raises(SafetyError, match="changed since the plan"):
        merger(world).execute(pid)
    assert world.lib() == lib0


def test_tracks_already_in_place_only_get_their_template_recorded(world: World) -> None:
    with world.sessions.begin() as s:
        s.execute(update(Track).values(path_template="{title}.{ext}"))
    lib0, m = world.lib(), merger(world, world.merger.template)
    pid = m.build_reorganize()
    assert {o["op"] for o in world.plan(pid).ops} == {"keep"}
    m.execute(pid)
    assert world.lib() == lib0 and world.trashed() == [] and drift(world, m.template) == 0
    m.undo_plan(pid)
    assert {r["path_template"] for r in rows(world)} == {"{title}.{ext}"}


def test_startup_backfill_labels_old_rows_without_moving_anything(world: World) -> None:
    with world.sessions.begin() as s:
        s.execute(update(Track).values(path_template=None))
    assert drift(world) == 0  # unlabelled rows are not reported
    lib0, m = world.lib(), merger(world)
    m.backfill_templates()
    assert world.lib() == lib0 and {r["path_template"] for r in rows(world)} == {T2}
    with pytest.raises(LookupError):
        m.build_reorganize()


def test_a_collision_with_a_track_already_in_place_gets_a_suffix(world: World) -> None:
    taken = "Band/2000 - Album/01 Song r1.flac"
    (world.ops.library / taken).parent.mkdir(parents=True)
    (world.ops.library / taken).write_bytes(b"in place")
    with world.sessions.begin() as s:
        s.add(
            Track(path=taken, status="verified", batch_id="x", imported_at=SystemClock().now(),
                  path_template=T2)
        )  # fmt: skip
    pid = merger(world).build_reorganize()
    moves = {o["old"]["path"]: o["new_path"] for o in world.plan(pid).ops}
    assert moves["Band/Album (2000)/01-01 Song r1.flac"] == "Band/2000 - Album/01 Song r1 (2).flac"
    assert taken not in moves.values()


def _plan_updates(w: World) -> list[str]:
    """Record every UPDATE of the plans table, by the columns it sets."""
    seen: list[str] = []

    def spy(_c: Any, _cur: Any, statement: str, *_: Any) -> None:
        if m := re.match(r"\s*UPDATE plans SET (.*?) WHERE", statement, re.S):
            seen.append(m[1])

    event.listen(w.sessions.kw["bind"], "before_cursor_execute", spy)
    return seen


def test_running_and_undoing_never_rewrite_the_ops(world: World) -> None:
    m = merger(world)
    pid = m.build_reorganize()
    ops0, writes = world.plan(pid).ops, _plan_updates(world)
    m.execute(pid)
    assert [o["done"] for o in world.plan_ops(pid)] == [True] * 5
    m.undo_plan(pid)
    assert [o["done"] for o in world.plan_ops(pid)] == [False] * 5
    assert any("status" in w for w in writes)  # the spy sees plan updates
    assert not [w for w in writes if re.search(r"\bops\b", w)] and world.plan(pid).ops == ops0


def test_a_crash_mid_plan_resumes_without_redoing_done_moves(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib0, m = world.lib(), merger(world)
    pid = m.build_reorganize()
    test_merge.crash_once(monkeypatch, "_commit_op", nth=3)  # dies before the third op commits
    with pytest.raises(test_merge.Boom):
        m.execute(pid)
    assert [o["done"] for o in world.plan_ops(pid)] == [True, True, False, False, False]
    m.execute(pid)
    assert world.plan(pid).status == "done"
    assert world.lib() == {new: lib0[old] for old, new in MOVES.items()}
    with world.sessions() as s:
        trashes = s.scalars(
            select(FileOp.src).where(FileOp.batch_id == f"reorganize-{pid}", FileOp.kind == "trash")
        ).all()
    assert sorted(trashes) == sorted(MOVES)  # each original moved exactly once


def test_a_merge_refused_for_a_template_change_leaves_the_canonical_alone(world: World) -> None:
    pid = test_merge.build(world)
    lib0 = world.lib()
    with pytest.raises(SafetyError, match="template changed"):
        merger(world).execute(pid)
    assert world.plan(pid).status == "failed" and world.canonical() is None
    assert world.lib() == lib0


def test_undo_of_a_plan_stored_before_path_template(world: World) -> None:
    pid = test_merge.build(world)
    with world.sessions.begin() as s:  # as a merge plan built before the column existed
        plan = s.get_one(Plan, pid)
        plan.ops = [
            {**o, "old": {k: v for k, v in o["old"].items() if k != "path_template"}}
            for o in plan.ops
        ]
    lib0, paths0 = world.lib(), sorted(r["path"] for r in rows(world))
    world.merger.execute(pid)
    world.merger.undo_plan(pid)
    assert world.plan(pid).status == "undone", world.plan(pid).error
    assert world.lib() == lib0 and sorted(r["path"] for r in rows(world)) == paths0


def test_undo_leaves_the_template_of_an_in_place_track_changed_since(world: World) -> None:
    with world.sessions.begin() as s:
        s.execute(update(Track).values(path_template="{title}.{ext}"))
    m = merger(world, world.merger.template)
    pid = m.build_reorganize()
    m.execute(pid)
    victim = "Band/Album (2000)/01-01 Song r1.flac"
    with world.sessions.begin() as s:
        s.execute(update(Track).where(Track.path == victim).values(acoustid_id="retagged"))
    m.undo_plan(pid)
    plan = world.plan(pid)
    assert plan.status == "undone" and victim in str(plan.error)
    templates = {r["path"]: r["path_template"] for r in rows(world)}
    assert templates.pop(victim) == m.template
    assert set(templates.values()) == {"{title}.{ext}"}


def _client(w: World, m: Merger) -> TestClient:
    w.merger = m  # _drain runs w.merger's handlers, as the worker would
    with w.sessions.begin() as s:
        create_user(s, "root", "pw", admin=True)
    app = create_app(w.sessions, Settings(path_template=T2))
    app.state.services = replace(app.state.services, merger=m)
    return TestClient(app, follow_redirects=False)


def _plan(c: TestClient, tok: str, w: World) -> int:
    """Ask for a plan, let the worker build it, and follow the status page to it."""
    r = c.post("/library/reorganize/plan", data={"csrf_token": tok})
    assert (r.status_code, r.headers["location"]) == (303, "/library/reorganize"), r.text
    _drain(w)
    found = re.search(r'href="/library/plans/(\d+)"', c.get("/library/reorganize").text)
    assert found
    return int(found[1])


def test_the_request_only_queues_the_build(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[Path] = []
    real = tags.read_fields
    monkeypatch.setattr(tags, "read_fields", lambda p: reads.append(p) or real(p))
    with _client(world, merger(world)) as c:
        tok = _login(c)
        r = c.post("/library/reorganize/plan", data={"csrf_token": tok})
        assert r.status_code == 303 and reads == []
        assert c.post("/library/reorganize/plan", data={"csrf_token": tok}).status_code == 303
        with world.sessions() as s:
            jobs = s.scalars(select(Job).where(Job.kind == JobKind.REORGANIZE_PLAN)).all()
            assert len(jobs) == 1 and s.scalar(select(Plan.id)) is None
        page = c.get("/library/reorganize").text
        assert "Building the reorganize plan" in page and 'hx-trigger="every 5s' in page
        _drain(world)
        assert len(reads) == 5
        live = c.get("/library/reorganize", headers={"HX-Request": "true"}).text
        assert "review reorganize plan" in live and "hx-trigger" not in live
        assert "<html" not in live  # the fragment alone


def test_a_build_with_nothing_to_move_says_why(world: World) -> None:
    with _client(world, world.merger) as c:  # every track already follows the template
        assert c.post("/library/reorganize/plan", data={"csrf_token": _login(c)}).status_code == 303
        _drain(world)
        assert "No plan was built" in c.get("/library/reorganize").text


def test_banner_shows_until_the_confirmed_plan_runs(world: World) -> None:
    m = merger(world)
    with _client(world, m) as c:
        tok = _login(c)
        page = c.get("/library/albums").text
        assert "Layout differs from the path template (5 files)" in page
        assert 'action="/library/reorganize/plan"' in page
        lib0 = world.lib()
        pid = _plan(c, tok, world)
        page = c.get(f"/library/plans/{pid}").text
        assert "Reorganize plan" in page and "Reorganize 5 files" in page
        assert "Band/Album (2000): 4 files" in page and "and 1 more" in page  # a sample per album
        assert world.lib() == lib0  # building and viewing move nothing
        assert c.post(f"/library/plans/{pid}/confirm", data={"csrf_token": tok}).status_code == 303
        _drain(world)
        assert world.plan(pid).status == "done", world.plan(pid).error
        assert "Layout differs" not in c.get("/library/albums").text


def test_every_move_is_listed_before_confirm(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(library_routes, "MOVES_PAGE", 2)  # five rows over three pages
    with _client(world, merger(world, device=other_mount)) as c:
        pid = _plan(c, _login(c), world)
        summary = c.get(f"/library/plans/{pid}").text
        assert "and 1 more" in summary and f'href="/library/plans/{pid}/moves"' in summary
        seen, url = "", f"/library/plans/{pid}/moves"
        while url:
            page = c.get(url).text
            seen += page
            nxt = re.search(r'href="([^"]+)">Next<', page)
            url = nxt[1] if nxt else ""
    for old, new in MOVES.items():  # the refused one shows where it would have gone
        assert old in seen and new in seen
    assert "refused: on another filesystem" in seen
    assert world.plan(pid).status == "draft"


def test_plan_page_reports_a_refused_move(world: World) -> None:
    with _client(world, merger(world, device=other_mount)) as c:
        pid = _plan(c, _login(c), world)
        page = c.get(f"/library/plans/{pid}").text
        assert "Reorganize 4 files" in page
        assert re.search(r"refused: Band/Album \(1999\)/01-03 Song r3.flac \(on another", page)
