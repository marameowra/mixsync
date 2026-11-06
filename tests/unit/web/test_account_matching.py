"""The user's auto-accept limit on the account page, and the admin's app-wide minimum."""

import re
from collections.abc import Iterator
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.requests import profile_for
from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.core.matching import Profile
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.auth import MatchingProfile, User
from mixsync.match.profiles import BALANCED

Row = tuple[int | None, str, float, float]


@pytest.fixture
def sf(settings: Settings) -> sessionmaker[Session]:
    upgrade()
    sf = make_session_factory(make_engine(settings))
    with sf() as s, s.begin():
        create_user(s, "alice", "pw")
        create_user(s, "bob", "pw")
        create_user(s, "root", "pw", admin=True)
    return sf


@pytest.fixture
def client(sf: sessionmaker[Session]) -> Iterator[TestClient]:
    with TestClient(create_app(sf, Settings()), follow_redirects=False) as c:
        yield c


def login(c: TestClient, user: str) -> str:
    c.cookies.clear()
    assert c.post("/login", data={"username": user, "password": "pw"}).status_code == 303
    m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', c.get("/account").text)
    assert m
    return m.group(1)


def set_limit(c: TestClient, tok: str, value: str) -> int:
    return c.post("/account/matching", data={"csrf_token": tok, "auto_accept": value}).status_code


def set_cap(c: TestClient, tok: str, value: str) -> int:
    return c.post("/account/cap", data={"csrf_token": tok, "minimum": value}).status_code


def row(sf: sessionmaker[Session], user: str) -> Row:
    with sf() as s:
        u = s.query(User).filter_by(username=user).one()
        p = s.get(MatchingProfile, u.matching_profile_id) if u.matching_profile_id else None
        return (p.id, p.preset, p.auto_accept_max, p.review_max) if p else (None, "", 0.0, 0.0)


def effective(sf: sessionmaker[Session], user: str) -> Profile:
    with sf() as s:
        return profile_for(s, s.query(User).filter_by(username=user).one().id)


def test_the_limits_show_as_confidence_and_a_save_makes_an_own_custom_row(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    tok = login(client, "alice")
    page = client.get("/account").text
    assert "Matches at 92% confidence" in page and "From 60% they wait" in page  # balanced
    assert set_limit(client, tok, "85") == 303
    pid, preset, auto, review = row(sf, "alice")
    assert pid is not None and (preset, auto, review) == ("custom", 0.15, 0.40)
    assert "Matches at 85% confidence" in client.get("/account").text
    assert row(sf, "bob")[0] is None


@pytest.mark.parametrize("value", ["59", "101", "-1", "abc", " ", "85.5"])
def test_an_out_of_range_limit_changes_nothing(
    client: TestClient, sf: sessionmaker[Session], value: str
) -> None:
    tok = login(client, "alice")
    set_limit(client, tok, "85")
    before = row(sf, "alice")
    r = client.post("/account/matching", data={"csrf_token": tok, "auto_accept": value})
    assert r.status_code == 400 and "Enter a whole number from 60 to 100." in r.text
    assert row(sf, "alice") == before


def test_a_shared_profile_row_is_copied_not_changed(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    with sf.begin() as s:
        shared = MatchingProfile(
            name="loose", preset="loose", auto_accept_max=0.15, review_max=0.55
        )
        s.add(shared)
        s.flush()
        for u in s.query(User):
            u.matching_profile_id = shared.id
        sid = shared.id
    assert set_limit(client, login(client, "alice"), "90") == 303
    mine = row(sf, "alice")
    assert mine[0] != sid and mine[1:] == ("custom", 0.10, 0.55)  # review_max carried over
    assert row(sf, "bob") == (sid, "loose", 0.15, 0.55)
    assert effective(sf, "bob") == Profile("loose", 0.15, 0.55)


def test_only_admins_see_and_set_the_cap(client: TestClient, sf: sessionmaker[Session]) -> None:
    tok = login(client, "alice")
    assert "/account/cap" not in client.get("/account").text
    assert client.get("/account/cap").status_code == 403
    assert set_cap(client, tok, "95") == 403
    assert effective(sf, "alice") == BALANCED  # no cap: today's behavior
    tok = login(client, "root")
    assert "/account/cap" in client.get("/account").text
    assert "No minimum is set." in client.get("/account/cap").text
    r = client.post("/account/cap", data={"csrf_token": tok, "minimum": "x"})
    assert r.status_code == 400 and "No minimum is set." in r.text
    assert set_cap(client, tok, "95") == 303
    assert "Current minimum: 95%." in client.get("/account/cap").text


def test_a_cap_clamps_stored_rows_and_the_preset_fallback(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    set_limit(client, login(client, "alice"), "85")
    tok = login(client, "root")
    set_cap(client, tok, "90")
    assert row(sf, "alice")[2] == 0.10  # 0.15 was looser than the cap
    assert effective(sf, "bob") == BALANCED  # 0.08 is already tighter
    set_cap(client, tok, "95")
    assert row(sf, "alice")[2] == 0.05
    assert effective(sf, "bob") == replace(BALANCED, auto_accept_max=0.05)  # no row: clamped too
    tok = login(client, "alice")
    r = client.post("/account/matching", data={"csrf_token": tok, "auto_accept": "94"})
    assert r.status_code == 400 and "The administrator&#39;s minimum is 95%." in r.text
    assert row(sf, "alice")[2] == 0.05
    set_cap(client, login(client, "root"), "")
    assert row(sf, "alice")[2] == 0.05  # clearing the cap leaves clamped rows as they are
    assert effective(sf, "bob") == BALANCED


def pick(c: TestClient, tok: str, preset: str) -> int:
    return c.post(
        "/account/matching/preset", data={"csrf_token": tok, "preset": preset}
    ).status_code


def test_a_preset_is_stored_on_the_users_own_row_and_survives_a_reload(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    tok = login(client, "alice")
    page = client.get("/account").text
    assert "current: Balanced" in page and "Strict: auto-accept at 96%, review from 70%" in page
    assert row(sf, "alice")[0] is None  # no row until a choice is made
    assert pick(client, tok, "loose") == 303
    assert row(sf, "alice")[1:] == ("loose", 0.15, 0.55)
    assert "current: Loose" in client.get("/account").text
    assert effective(sf, "alice") == Profile("loose", 0.15, 0.55)  # new decisions band with it
    assert row(sf, "bob")[0] is None and effective(sf, "bob") == BALANCED
    assert pick(client, tok, "nope") == 400


def test_a_custom_edit_shows_custom_and_a_preset_replaces_it(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    tok = login(client, "alice")
    pick(client, tok, "strict")
    assert set_limit(client, tok, "90") == 303
    assert row(sf, "alice")[1:] == ("custom", 0.10, 0.30)
    assert "current: Custom" in client.get("/account").text
    pick(client, tok, "balanced")
    assert row(sf, "alice")[1:] == ("balanced", 0.08, 0.40)


def test_picking_copies_a_shared_row_and_a_cap_is_noted(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    with sf.begin() as s:
        shared = MatchingProfile(name="x", preset="strict", auto_accept_max=0.04, review_max=0.30)
        s.add(shared)
        s.flush()
        for u in s.query(User):
            u.matching_profile_id = shared.id
    pick(client, login(client, "alice"), "loose")
    assert row(sf, "bob")[1:] == ("strict", 0.04, 0.30)
    assert row(sf, "alice")[0] != row(sf, "bob")[0]
    set_cap(client, login(client, "root"), "92")
    login(client, "alice")
    assert "minimum of 92% limits auto-accept for Loose" in client.get("/account").text
    assert effective(sf, "alice").auto_accept_max == pytest.approx(0.08)


def test_a_preset_chosen_under_a_cap_is_stored_raw_and_clamped_on_load(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    set_cap(client, login(client, "root"), "92")
    pick(client, login(client, "alice"), "loose")
    assert row(sf, "alice")[1:] == ("loose", 0.15, 0.55)  # no UPDATE ran after the pick
    assert effective(sf, "alice").auto_accept_max == pytest.approx(0.08)
    assert "minimum of 92% limits auto-accept for Loose" in client.get("/account").text
