"""Two download sources: the search tries them in the admin's source priority, falls through
an empty one, shares one request budget, and backs off only when all came up empty."""

from dataclasses import replace

from mixsync.acquire.requests import candidate_from_dict
from mixsync.core.matching import TransferStatus
from mixsync.db.models.auth import AppSettings
from mixsync.db.models.work import Request

from .conftest import Env
from .fakes import FakeSource
from .test_pipeline import dropped, library, request, status, tried

FOLDER = "Music\\Alpha Band\\Alpha Album"


def second_source(env: Env, tmp_name: str = "scratch2") -> FakeSource:
    """A second FakeSource, "other", after Soulseek in the wired (default) order."""
    other = FakeSource(env.downloads, env.data_dir.parent / tmp_name)
    env.pipeline = replace(env.pipeline, sources={"slskd": env.source, "other": other})
    env.handlers = env.pipeline.handlers()
    return other


def save_priority(env: Env, rows: list[list[object]]) -> None:
    with env.sessions.begin() as s:
        s.merge(AppSettings(id=1, source_priority=rows))


async def test_the_default_priority_is_the_wired_order_soulseek_first(env: Env) -> None:
    other = second_source(env)
    env.source.add("peer1", FOLDER)
    other.add("idx1", f"{FOLDER} [other]")
    rid = request(env)
    await env.drain()
    assert env.source.enqueued == ["peer1"] and other.enqueued == other.queries == []
    assert status(env, rid) == "imported"


async def test_the_saved_priority_is_read_at_each_search(env: Env) -> None:
    other = second_source(env)
    env.source.add("peer1", FOLDER)
    other.add("idx1", f"{FOLDER} [other]")
    save_priority(env, [["other", True], ["slskd", True]])
    rid = request(env)
    await env.drain()
    assert other.enqueued == ["idx1"] and env.source.enqueued == env.source.queries == []
    # the candidate remembers its source: status and release went to "other" alone
    assert other.cleared == ["idx1"] and env.source.cleared == []
    assert status(env, rid) == "imported" and len(library(env)) == 2


async def test_a_disabled_source_is_never_searched(env: Env) -> None:
    other = second_source(env)
    other.add("idx1", f"{FOLDER} [other]")
    save_priority(env, [["slskd", True], ["other", False]])
    rid = request(env)
    await env.step()
    assert other.queries == [] and status(env, rid) == "no_results"


async def test_an_empty_first_source_falls_through_to_the_next(env: Env) -> None:
    other = second_source(env)
    other.add("idx1", f"{FOLDER} [other]")
    rid = request(env)
    await env.drain()
    assert env.source.queries and other.enqueued == ["idx1"]
    with env.sessions() as s:
        assert s.get_one(Request, rid).no_results_attempts == 0
    assert status(env, rid) == "imported"


async def test_backoff_starts_only_when_every_source_came_up_empty(env: Env) -> None:
    other = second_source(env)
    rid = request(env)
    await env.step()
    assert env.source.queries and other.queries  # both were asked
    with env.sessions() as s:
        assert s.get_one(Request, rid).no_results_attempts == 1
    assert status(env, rid) == "no_results"


async def test_one_request_budget_is_shared_across_sources(env: Env) -> None:
    other = second_source(env)
    for i in range(3):  # every transfer dies after some bytes landed, on both sources
        env.source.add(f"peer{i}", f"{FOLDER} [{i}]")
        env.source.state[f"peer{i}"] = TransferStatus.FAILED
        env.source.landed[f"peer{i}"] = 100
        other.add(f"idx{i}", f"{FOLDER} [o{i}]")
        other.state[f"idx{i}"] = TransferStatus.FAILED
        other.landed[f"idx{i}"] = 100
    rid = request(env)
    await env.drain()
    assert status(env, rid) == "failed" and dropped(env, rid) == 5
    assert len(env.source.enqueued) == 3 and len(other.enqueued) == 2  # 5 in all, not 5 each
    # Soulseek keeps the bare [peer, folder] key; another source's key names the source
    assert ["peer0", f"{FOLDER} [0]"] in tried(env, rid)
    assert ["idx0", f"{FOLDER} [o0]", "other"] in tried(env, rid)


async def test_a_tried_soulseek_folder_does_not_hide_the_same_id_on_another_source(
    env: Env,
) -> None:
    other = second_source(env)
    env.source.add("peer1", FOLDER)
    other.add("peer1", FOLDER)
    env.source.state["peer1"] = TransferStatus.FAILED
    rid = request(env)
    await env.drain()
    assert tried(env, rid) == [["peer1", FOLDER]]
    assert other.enqueued == ["peer1"] and status(env, rid) == "imported"


def test_a_candidate_stored_before_sources_existed_loads_as_soulseek() -> None:
    file = {"path": "a.flac", "size": 1, "extension": "flac"}
    old = {"username": "peer1", "folder": FOLDER, "files": [file]}  # no "source" key
    assert candidate_from_dict(old).source == "slskd"
