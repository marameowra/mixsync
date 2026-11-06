"""A user's auto-accept limit, set on the account page, decides new stage 2 decisions."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mixsync.acquire.requests import create_request
from mixsync.db.auth import create_user
from mixsync.db.models.matching import MatchDecision

from .conftest import Env, login
from .fakes import ALBUM


def stage_two(env: Env) -> list[tuple[str, float]]:
    with env.sessions() as s:
        rows = s.scalars(select(MatchDecision).where(MatchDecision.stage == 2))
        return [(d.status, d.distance) for d in rows]


@pytest.mark.parametrize(("percent", "status"), [(90, "auto_accepted"), (91, "pending_review")])
async def test_the_users_limit_decides_the_band(
    env: Env, web: TestClient, percent: int, status: str
) -> None:
    tok = login(web, "alice")
    r = web.post("/account/matching", data={"csrf_token": tok, "auto_accept": str(percent)})
    assert r.status_code == 303
    # tags a year off: 0.091, over the balanced 0.08, under 0.10 (90%), over 0.09 (91%)
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album", album=replace(ALBUM, year=2019))
    with env.sessions.begin() as s:
        create_request(s, env.clock.now(), env.user_id, str(ALBUM.release_id))
    await env.drain()
    ((got, distance),) = stage_two(env)
    assert 0.09 < distance < 0.10 and got == status


async def test_a_new_limit_does_not_reband_a_waiting_review(env: Env, web: TestClient) -> None:
    env.source.add("peer1", "Music\\Alpha Band\\Alpha Album", album=replace(ALBUM, year=2019))
    with env.sessions.begin() as s:
        create_request(s, env.clock.now(), env.user_id, str(ALBUM.release_id))
    await env.drain()
    before = stage_two(env)
    assert [st for st, _ in before] == ["pending_review"]  # balanced: 0.091 is over 0.08
    tok = login(web, "alice")
    web.post("/account/matching", data={"csrf_token": tok, "auto_accept": "90"})
    with env.sessions.begin() as s:
        create_user(s, "root", "pw", admin=True)
    tok = login(web, "root")
    web.post("/account/cap", data={"csrf_token": tok, "minimum": "95"})
    await env.drain()
    assert stage_two(env) == before
