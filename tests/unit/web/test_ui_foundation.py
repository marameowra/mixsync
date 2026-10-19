import re
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.auth import Role, RoleCapability, UserRole
from mixsync.db.models.library import Track
from mixsync.db.models.matching import MatchDecision
from mixsync.web.templating import templates


@pytest.fixture
def sf(settings: Settings) -> sessionmaker[Session]:
    upgrade()
    return make_session_factory(make_engine(settings))


@pytest.fixture
def client(sf: sessionmaker[Session]) -> Iterator[TestClient]:
    with sf() as s, s.begin():
        alice = create_user(s, "alice", "pw")
        bob = create_user(s, "bob", "pw")
        create_user(s, "root", "pw", admin=True)
        for name, caps in (("none", []), ("editor", ["library.edit"])):
            user = create_user(s, name, "pw")
            role = Role(name=name)
            s.add(role)
            s.flush()
            s.execute(delete(UserRole).where(UserRole.user_id == user.id))
            s.add(UserRole(user_id=user.id, role_id=role.id))
            for cap in caps:
                s.add(RoleCapability(role_id=role.id, capability=cap))
        for owner, stage, status in (
            (alice.id, 2, "pending_review"),
            (bob.id, 2, "pending_review"),
            (alice.id, 1, "pending_review"),
            (alice.id, 2, "accepted"),
        ):
            s.add(
                MatchDecision(
                    user_id=owner,
                    stage=stage,
                    release_mbid="",
                    band="review",
                    distance=0.2,
                    breakdown=[],
                    vetoes=[],
                    scorer_version=1,
                    evidence={},
                    status=status,
                )
            )
        s.add(
            Track(
                path="album/track.flac",
                status="unverified",
                batch_id="test",
                imported_at=datetime.now(UTC),
            )
        )
    with TestClient(create_app(sf), follow_redirects=False, headers={"accept": "text/html"}) as c:
        yield c


def login(client: TestClient, username: str) -> None:
    client.cookies.clear()
    assert client.post("/login", data={"username": username, "password": "pw"}).status_code == 303


def nav(html: str) -> str:
    return html.split('<nav class="nav"', 1)[1].split("</nav>", 1)[0]


@pytest.mark.parametrize(
    ("username", "shown", "hidden"),
    [
        (
            "alice",
            ["Find", "Resume", "/discover", "/requests", "/review"],
            ["Library", "/library/fragments", "/library/unverified", "/migration"],
        ),
        (
            "editor",
            ["Resume", "Library", "/library/fragments", "/library/unverified"],
            ["Find", "/requests", "/review", "/migration"],
        ),
        ("none", ["/account", "/logout"], ["Find", "Resume", "Library", "/requests", "/review"]),
        ("root", ["Find", "Resume", "Library", "/migration"], []),
    ],
)
def test_navigation_follows_route_capabilities(
    client: TestClient, username: str, shown: list[str], hidden: list[str]
) -> None:
    login(client, username)
    response = client.get("/")
    assert response.status_code == 200
    menu = nav(response.text)
    for item in shown:
        assert item in menu
    for item in hidden:
        assert item not in menu
    assert 'class="brand" href="/" aria-current="page"' in menu
    assert 'name="csrf_token"' in menu


@pytest.mark.parametrize(("username", "count"), [("alice", 1), ("bob", 1), ("root", 2)])
def test_waiting_counts_follow_review_ownership_on_every_page(
    client: TestClient, username: str, count: int
) -> None:
    login(client, username)
    home = client.get("/").text
    assert f"{count} item" in home and "awaiting review" in home
    assert 'class="btn primary" href="/review"' in home
    account = client.get("/account")
    assert account.status_code == 200
    menu = nav(account.text)
    assert f'Review<span class="count">{count}</span>' in menu
    assert 'href="/account" aria-current="page"' in menu
    assert 'href="/review" aria-current=' not in menu


def test_editor_home_uses_available_work_and_count(client: TestClient) -> None:
    login(client, "editor")
    home = client.get("/").text
    assert "1 unverified track" in home
    assert 'Unverified<span class="count">1</span>' in nav(home)
    assert 'class="btn primary" href="/library/unverified"' in home
    assert 'href="/review"' not in home


def test_home_without_capabilities_has_no_inaccessible_work(client: TestClient) -> None:
    login(client, "none")
    home = client.get("/").text
    assert "No work is waiting for this account." in home
    assert 'class="btn primary"' not in home
    assert "unverified track" not in home and "awaiting review" not in home


def test_current_navigation_matches_nested_pages(client: TestClient) -> None:
    login(client, "alice")
    response = client.get("/review/1")
    assert response.status_code == 200
    assert 'href="/review" aria-current="page"' in nav(response.text)


def test_non_html_authenticated_request_skips_navigation_counts(client: TestClient) -> None:
    login(client, "alice")
    response = client.get("/account", headers={"accept": "application/json"})
    assert response.status_code == 200
    assert 'Review<span class="count">' not in nav(response.text)
    assert 'Unverified<span class="count">' not in nav(response.text)


@pytest.mark.parametrize(
    ("value", "glyph", "word", "tone"),
    [
        ("auto_accept", "●", "Confident", "confident"),
        ("review", "◐", "Needs review", "review"),
        ("rejected", "✕", "Rejected", "reject"),
        ("owned", "●", "Already in your library", "confident"),
        ("kept", "●", "Kept", "confident"),
        ("skipped_cap", "◐", "Weekly limit reached", "review"),
        ("skipped_filter", "◐", "Filtered out", "review"),
        ("not found", "◐", "Not found", "review"),
        ("pending", "○", "Pending", "neutral"),
        ("<script>", "○", "&lt;script&gt;", "neutral"),
    ],
)
def test_status_has_readable_text_glyph_and_safe_unknown_fallback(
    value: str, glyph: str, word: str, tone: str
) -> None:
    template = templates.env.from_string(
        '{% import "macros/ui.html" as ui %}{{ ui.status(value) }}'
    )
    html = template.render(value=value)
    assert f"tag status {tone}" in html
    assert f'aria-hidden="true">{glyph}</span>' in html
    assert word in html
    assert re.search(r"<script>", html) is None
