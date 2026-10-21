import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.auth import Role, RoleCapability, User, UserRole
from mixsync.db.models.discovery import DiscoveryPlaylist, ProvisionalHold, ProvisionalTrack
from mixsync.db.models.library import Track
from mixsync.db.models.matching import MatchDecision
from mixsync.db.models.work import Migration, Request
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
    assert "Recent requests" not in home and "New in Discover" not in home
    assert "In progress" not in home


def test_home_shows_only_owned_current_requests_and_active_discover_holds(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    now = datetime.now(UTC)
    with sf() as s, s.begin():
        alice = s.query(User).filter_by(username="alice").one()
        bob = s.query(User).filter_by(username="bob").one()
        migration = Migration(user_id=alice.id, status="running")
        alice_playlist = DiscoveryPlaylist(user_id=alice.id, name="Alice")
        bob_playlist = DiscoveryPlaylist(user_id=bob.id, name="Bob")
        s.add_all([migration, alice_playlist, bob_playlist])
        s.flush()
        for n in range(6):
            s.add(
                Request(
                    user_id=alice.id,
                    release_mbid=f"release-{n}",
                    album=f"Own {n}",
                    status="searching",
                )
            )
        s.add_all(
            [
                Request(
                    user_id=bob.id, release_mbid="other", album="Other user", status="searching"
                ),
                Request(
                    user_id=alice.id,
                    release_mbid="migration",
                    album="Migration request",
                    status="searching",
                    migration_id=migration.id,
                ),
                Request(
                    user_id=alice.id,
                    release_mbid="failed",
                    album="Failed release",
                    status="failed",
                    last_error="No source found",
                ),
            ]
        )
        for n in range(7):
            track = ProvisionalTrack(
                path=f"discover-{n}.flac",
                recording_mbid=f"recording-{n}",
                batch_id="test",
                imported_at=now + (timedelta(days=3) if n == 1 else timedelta(minutes=n)),
                status="active",
                title=f"Track {n}" if n != 6 else None,
                artist="Artist" if n != 6 else None,
            )
            s.add(track)
            s.flush()
            s.add(
                ProvisionalHold(
                    user_id=alice.id,
                    recording_mbid=f"recording-{n}",
                    playlist_id=alice_playlist.id,
                    provisional_track_id=track.id,
                    expires_at=now + timedelta(days=4),
                    expired_at=now if n == 1 else None,
                )
            )
        other_track = ProvisionalTrack(
            path="other.flac",
            recording_mbid="other-recording",
            batch_id="test",
            imported_at=now + timedelta(days=4),
        )
        s.add(other_track)
        s.flush()
        s.add(
            ProvisionalHold(
                user_id=bob.id,
                recording_mbid="other-recording",
                playlist_id=bob_playlist.id,
                provisional_track_id=other_track.id,
                expires_at=now + timedelta(days=4),
            )
        )
        inactive_track = ProvisionalTrack(
            path="inactive.flac",
            recording_mbid="inactive-recording",
            batch_id="test",
            imported_at=now + timedelta(days=3),
            status="trashed",
            title="Inactive track",
        )
        s.add(inactive_track)
        s.flush()
        s.add(
            ProvisionalHold(
                user_id=alice.id,
                recording_mbid="inactive-recording",
                playlist_id=alice_playlist.id,
                provisional_track_id=inactive_track.id,
                expires_at=now + timedelta(days=4),
            )
        )
        alice_playlist.entries = [
            {"recording_mbid": "recording-6", "title": "Legacy title", "artist": "Legacy artist"}
        ]

    login(client, "alice")
    home = client.get("/").text
    for n in range(2, 6):
        assert f"Own {n}" in home
    for hidden in ["Own 0", "Own 1", "Other user", "Migration request"]:
        assert hidden not in home
    assert "Failed release" in home and "No source found" in home
    for n in (5, 4, 3, 2):
        assert f"Track {n}" in home
    assert "Legacy title" in home and "Legacy artist" in home
    for hidden in ("Track 0", "Track 1", "other-recording", "Inactive track"):
        assert hidden not in home
    assert home.count("Expires ") == 5


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
