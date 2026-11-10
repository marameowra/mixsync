"""Automatic downloads off: requests wait in the approval queue (#296)."""

from fastapi.testclient import TestClient
from sqlalchemy import select

from mixsync.acquire.requests import create_request, deny
from mixsync.core.jobs import JobKind
from mixsync.db.auth import create_user
from mixsync.db.models.auth import User
from mixsync.db.models.discovery import MirroredPlaylist, ProvisionalHold
from mixsync.db.models.work import Request
from mixsync.discovery import mirrored

from .conftest import Env, login
from .fakes import ALBUM
from .test_discovery_playlists import Rig, ref
from .test_mirrored_playlists import REL, MirrorRig, up
from .test_watchlist import FakeWatch, entity_id, follow, poll, rg, today


def auto(env: Env, on: bool, user_id: int | None = None) -> None:
    with env.sessions.begin() as s:
        s.get_one(User, user_id or env.user_id).download_auto = on


def make_admin(env: Env) -> int:
    with env.sessions.begin() as s:
        return create_user(s, "root", "pw", admin=True).id


def manual(env: Env, user_id: int | None = None) -> int:
    with env.sessions.begin() as s:
        return create_request(s, env.clock.now(), user_id or env.user_id, str(ALBUM.release_id)).id


def req(env: Env, rid: int) -> Request:
    with env.sessions() as s:
        return s.get_one(Request, rid)


async def test_a_request_without_automatic_downloads_is_parked_with_no_search(env: Env) -> None:
    auto(env, False)
    rid = manual(env)
    assert req(env, rid).status == "pending_approval"
    assert env.jobs(JobKind.SEARCH) == []
    await env.drain()
    assert req(env, rid).status == "pending_approval"


def test_automatic_downloads_on_searches_at_once(env: Env) -> None:
    rid = manual(env)
    assert req(env, rid).status == "searching"
    assert len(env.jobs(JobKind.SEARCH)) == 1


def test_an_admin_is_never_parked_whatever_the_flag(env: Env) -> None:
    root = make_admin(env)
    auto(env, False, root)
    assert req(env, manual(env, root)).status == "searching"


async def test_approve_queues_the_search_and_the_request_imports(env: Env, web: TestClient) -> None:
    auto(env, False)
    make_admin(env)
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album")
    rid = manual(env)
    tok = login(web, "root")
    page = web.get("/approvals")
    assert page.status_code == 200 and "alice" in page.text
    assert f"/approvals/{rid}/approve" in page.text
    assert web.post(f"/approvals/{rid}/approve", data={"csrf_token": tok}).status_code == 303
    assert len(env.jobs(JobKind.SEARCH)) == 1
    again = web.post(f"/approvals/{rid}/approve", data={"csrf_token": tok})
    assert again.status_code == 409 and len(env.jobs(JobKind.SEARCH)) == 1
    await env.drain()
    assert req(env, rid).status == "imported"


def test_deny_ends_it_with_the_reason_shown_to_the_requester(env: Env, web: TestClient) -> None:
    auto(env, False)
    make_admin(env)
    rid = manual(env)
    tok = login(web, "root")
    data = {"csrf_token": tok, "reason": "not on this server"}
    assert web.post(f"/approvals/{rid}/deny", data=data).status_code == 303
    assert req(env, rid).status == "denied"
    assert env.jobs(JobKind.SEARCH) == []
    assert f"/approvals/{rid}/" not in web.get("/approvals").text
    login(web, "alice")
    page = web.get("/requests").text
    assert "Denied" in page and "not on this server" in page
    again = web.post(f"/approvals/{rid}/deny", data=data)
    assert again.status_code == 403  # alice has no approve
    tok = login(web, "root")
    again = web.post(f"/approvals/{rid}/deny", data={"csrf_token": tok})
    assert again.status_code == 409 and req(env, rid).last_error == "not on this server"
    assert web.post(f"/approvals/{rid}/approve", data={"csrf_token": tok}).status_code == 409
    assert env.jobs(JobKind.SEARCH) == []


def test_nav_counts_pending_requests_for_approvers_only(env: Env, web: TestClient) -> None:
    auto(env, False)
    make_admin(env)
    manual(env)
    manual(env)
    login(web, "root")
    assert 'href="/approvals">Approvals<span class="count">2</span>' in web.get("/").text
    login(web, "alice")
    assert 'href="/approvals"' not in web.get("/").text
    assert "Awaiting approval" in web.get("/requests").text


def test_without_approve_the_queue_and_its_actions_are_403(env: Env, web: TestClient) -> None:
    auto(env, False)
    rid = manual(env)
    tok = login(web, "alice")
    assert web.get("/approvals").status_code == 403
    for action in ("approve", "deny"):
        r = web.post(f"/approvals/{rid}/{action}", data={"csrf_token": tok})
        assert r.status_code == 403
    assert req(env, rid).status == "pending_approval"


def test_the_owner_can_delete_a_parked_request(env: Env, web: TestClient) -> None:
    auto(env, False)
    rid = manual(env)
    tok = login(web, "alice")
    assert web.post(f"/requests/{rid}/delete", data={"csrf_token": tok}).status_code == 303
    with env.sessions() as s:
        assert s.get(Request, rid) is None


def test_turning_it_back_on_leaves_parked_requests_in_the_queue(env: Env) -> None:
    auto(env, False)
    rid = manual(env)
    auto(env, True)
    assert req(env, rid).status == "pending_approval"
    assert env.jobs(JobKind.SEARCH) == []
    assert req(env, manual(env)).status == "searching"


async def test_watchlist_requests_are_parked(env: Env) -> None:
    auto(env, False)
    fid, watch = follow(env), FakeWatch()
    watch.hits = [rg(1, today(env))]
    await poll(env, watch, entity_id(env, fid))
    with env.sessions() as s:
        (r,) = s.scalars(select(Request)).all()
    assert (r.source, r.status) == ("watchlist", "pending_approval")
    assert env.jobs(JobKind.SEARCH) == []


async def test_discovery_requests_are_parked(env: Env) -> None:
    auto(env, False)
    rig = Rig(env)
    rig.link(env.user_id)
    rig.playlist()
    rig.history.weekly = [ref(1)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    with env.sessions() as s:
        (r,) = s.scalars(select(Request)).all()
    assert (r.source, r.status) == ("discovery", "pending_approval")
    assert env.jobs(JobKind.SEARCH) == []


def bob(env: Env) -> int:
    with env.sessions.begin() as s:
        return create_user(s, "bob", "pw").id


def provisional_requests(env: Env) -> list[Request]:
    with env.sessions() as s:
        return list(s.scalars(select(Request).order_by(Request.id)))


async def test_another_users_parked_request_does_not_stand_in_for_bobs(env: Env) -> None:
    auto(env, False)
    b = bob(env)
    rig = Rig(env)
    for uid in (env.user_id, b):
        rig.link(uid)
        rig.playlist(uid)
    rig.history.weekly = [ref(1)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)  # alice's: parked
    await rig.run(JobKind.DISCOVERY_BUILD)  # bob's: his own, searching
    got = [(r.user_id, r.status) for r in provisional_requests(env)]
    assert got == [(env.user_id, "pending_approval"), (b, "searching")]
    with env.sessions() as s:
        holds = {h.request_id for h in s.scalars(select(ProvisionalHold))}
    assert holds == {r.id for r in provisional_requests(env)}  # each hold on its owner's


async def test_a_denied_discovery_track_is_not_asked_for_again(env: Env) -> None:
    auto(env, False)
    rig = Rig(env)
    rig.link(env.user_id)
    rig.playlist()
    rig.history.weekly = [ref(1)]
    rig.disc.start_chains()
    await rig.run(JobKind.DISCOVERY_BUILD)
    (r,) = provisional_requests(env)
    with env.sessions.begin() as s:
        deny(s.get_one(Request, r.id), "no")
    await rig.run(JobKind.DISCOVERY_BUILD)  # the next build in the chain
    assert [(q.id, q.status) for q in provisional_requests(env)] == [(r.id, "denied")]


async def test_a_mirror_still_lists_a_track_another_user_is_waiting_on(env: Env) -> None:
    auto(env, False)
    b = bob(env)
    with env.sessions.begin() as s:  # alice's request for the release, parked
        create_request(s, env.clock.now(), env.user_id, REL)
    mr = MirrorRig(env)
    mr.base.link(b, ("navidrome",))
    pid = mr.create(b)
    mr.upstream.lists["a"] = [up(1)]
    await mr.sync()
    with env.sessions() as s:
        states = [st for _, st in mirrored.track_states(s, s.get_one(MirroredPlaylist, pid))]
    assert states == [mirrored.MISSING]
    with env.sessions.begin() as s:
        assert mirrored.request_missing(s, env.clock.now(), s.get_one(MirroredPlaylist, pid), {REL})
    mine = [r for r in provisional_requests(env) if r.user_id == b]
    assert [(r.release_mbid, r.status) for r in mine] == [(REL, "searching")]
