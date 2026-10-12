import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.core.errors import TransientError
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.accounts import UserLink
from mixsync.db.models.auth import User
from mixsync.db.models.discovery import DiscoveryPlaylist
from mixsync.db.secretbox import SecretBox, link_secret, load_box
from mixsync.discovery.listenbrainz import ListenBrainzClient
from mixsync.ratelimit.client import polite_client
from mixsync.web.routes import account
from mixsync.web.routes.account import DefaultValidator, Ping, get_link

SECRET = "s3cret-token-XYZ"


class FakeValidator:
    def __init__(self) -> None:
        self.ok = True
        self.ping = Ping.ok
        self.lb_down = False

    async def listenbrainz_username(self, token: str) -> str | None:
        if self.lb_down:
            raise TransientError("down")
        return "lb-real-name" if self.ok else None

    async def navidrome_ping(self, username: str, password: str) -> Ping:
        return self.ping if self.ok else Ping.rejected


@pytest.fixture
def sf(settings: Settings) -> sessionmaker[Session]:
    upgrade()
    sf = make_session_factory(make_engine(settings))
    with sf() as s, s.begin():
        create_user(s, "alice", "pw")
        create_user(s, "bob", "pw")
    return sf


@pytest.fixture
def fake() -> FakeValidator:
    return FakeValidator()


@pytest.fixture
def box(tmp_path: Path) -> SecretBox:
    return load_box(Settings(config_dir=str(tmp_path)))  # generates tmp_path/secret.key


@pytest.fixture
def client(sf: sessionmaker[Session], fake: FakeValidator, box: SecretBox) -> Iterator[TestClient]:
    app = create_app(sf, Settings())
    app.state.link_validator = fake
    app.state.secret_box = box
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _login(c: TestClient, user: str) -> str:
    c.cookies.clear()
    assert c.post("/login", data={"username": user, "password": "pw"}).status_code == 303
    m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', c.get("/account").text)
    assert m
    return m.group(1)


def _link(
    sf: sessionmaker[Session], box: SecretBox, user: str, service: str
) -> tuple[str, str | None] | None:
    with sf() as s:
        uid = s.query(User).filter_by(username=user).one().id
        link = get_link(s, uid, service)
        return (link.external_username, link_secret(box, link)) if link else None


def test_save_and_display_never_leaks_secret(
    client: TestClient, sf: sessionmaker[Session], box: SecretBox
) -> None:
    tok = _login(client, "alice")
    r = client.post("/account/listenbrainz", data={"secret": SECRET, "csrf_token": tok})
    assert r.status_code == 303
    assert _link(sf, box, "alice", "listenbrainz") == (
        "lb-real-name",
        SECRET,
    )  # validated name wins
    r = client.post(
        "/account/navidrome", data={"username": "ali", "secret": SECRET, "csrf_token": tok}
    )
    assert r.status_code == 303
    page = client.get("/account")
    assert "Linked as lb-real-name" in page.text and "Linked as ali" in page.text
    assert "Replace" in page.text and "Unlink" in page.text
    assert SECRET not in page.text and SECRET not in r.text


def test_linking_listenbrainz_creates_the_default_playlist_once(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    tok = _login(client, "alice")
    client.post("/account/navidrome", data={"username": "a", "secret": "x", "csrf_token": tok})
    with sf() as s:
        assert s.query(DiscoveryPlaylist).count() == 0  # Navidrome alone makes none
    for _ in range(2):  # a relink adds no second one
        r = client.post("/account/listenbrainz", data={"secret": SECRET, "csrf_token": tok})
        assert r.status_code == 303
    with sf() as s:
        assert [p.name for p in s.query(DiscoveryPlaylist)] == ["Discovery"]


def test_failed_validation_stores_nothing(
    client: TestClient, sf: sessionmaker[Session], box: SecretBox, fake: FakeValidator
) -> None:
    tok = _login(client, "alice")
    fake.ok = False
    r = client.post("/account/listenbrainz", data={"secret": SECRET, "csrf_token": tok})
    assert r.status_code == 400 and "did not accept" in r.text and SECRET not in r.text
    r = client.post(
        "/account/navidrome", data={"username": "ali", "secret": SECRET, "csrf_token": tok}
    )
    assert r.status_code == 400 and "did not accept" in r.text and SECRET not in r.text
    assert (
        _link(sf, box, "alice", "listenbrainz") is None
        and _link(sf, box, "alice", "navidrome") is None
    )


def test_failed_replace_keeps_old_link(
    client: TestClient, sf: sessionmaker[Session], box: SecretBox, fake: FakeValidator
) -> None:
    tok = _login(client, "alice")
    client.post("/account/listenbrainz", data={"secret": SECRET, "csrf_token": tok})
    fake.ok = False
    client.post("/account/listenbrainz", data={"secret": "other", "csrf_token": tok})
    assert _link(sf, box, "alice", "listenbrainz") == ("lb-real-name", SECRET)


def test_users_are_isolated(client: TestClient, sf: sessionmaker[Session], box: SecretBox) -> None:
    tok = _login(client, "alice")
    client.post("/account/listenbrainz", data={"secret": SECRET, "csrf_token": tok})
    tok = _login(client, "bob")
    page = client.get("/account")
    assert "lb-real-name" not in page.text and "Not linked" in page.text
    client.post("/account/listenbrainz/unlink", data={"csrf_token": tok})  # touches only bob's
    assert _link(sf, box, "alice", "listenbrainz") == ("lb-real-name", SECRET)
    client.post("/account/listenbrainz", data={"secret": "bobs", "csrf_token": tok})
    assert _link(sf, box, "alice", "listenbrainz") == ("lb-real-name", SECRET)
    assert _link(sf, box, "bob", "listenbrainz") == ("lb-real-name", "bobs")


def test_csrf_required(client: TestClient, sf: sessionmaker[Session], box: SecretBox) -> None:
    _login(client, "alice")
    assert client.post("/account/listenbrainz", data={"secret": SECRET}).status_code == 403
    assert _link(sf, box, "alice", "listenbrainz") is None


def test_unlink_and_unknown_service(
    client: TestClient, sf: sessionmaker[Session], box: SecretBox
) -> None:
    tok = _login(client, "alice")
    client.post("/account/listenbrainz", data={"secret": SECRET, "csrf_token": tok})
    r = client.post("/account/listenbrainz/unlink", data={"csrf_token": tok})
    assert r.status_code == 303 and _link(sf, box, "alice", "listenbrainz") is None
    r = client.post("/account/lastfm", data={"secret": "x", "csrf_token": tok})
    assert r.status_code == 404


def test_requires_login(client: TestClient) -> None:
    assert client.get("/account").status_code == 303


def test_stored_value_is_encrypted_and_round_trips(
    client: TestClient, sf: sessionmaker[Session], box: SecretBox
) -> None:
    tok = _login(client, "alice")
    client.post("/account/listenbrainz", data={"secret": SECRET, "csrf_token": tok})
    with sf() as s:
        raw = s.query(UserLink).one()
        assert SECRET not in raw.secret_encrypted and not raw.needs_relink
        assert link_secret(box, raw) == SECRET


def test_wrong_key_marks_needs_relink_and_shows_banner(
    client: TestClient, sf: sessionmaker[Session], box: SecretBox
) -> None:
    tok = _login(client, "alice")
    client.post("/account/listenbrainz", data={"secret": SECRET, "csrf_token": tok})
    client.app.state.secret_box = SecretBox(Fernet.generate_key())  # type: ignore[attr-defined]
    page = client.get("/account")
    assert page.status_code == 200 and "can no longer be decrypted" in page.text
    assert "Needs relinking" in page.text and SECRET not in page.text
    with sf() as s:
        assert s.query(UserLink).one().needs_relink
    client.app.state.secret_box = box  # type: ignore[attr-defined]
    assert "can no longer be decrypted" not in client.get("/account").text  # key back: cleared


def test_double_insert_is_retried_as_update(
    client: TestClient,
    sf: sessionmaker[Session],
    box: SecretBox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tok = _login(client, "alice")
    client.post("/account/listenbrainz", data={"secret": "first", "csrf_token": tok})
    real, calls = account.get_link, []

    def blind_once(s: Session, user_id: int, service: str) -> UserLink | None:
        calls.append(1)
        return None if len(calls) == 1 else real(s, user_id, service)  # the racer's row is unseen

    monkeypatch.setattr(account, "get_link", blind_once)
    r = client.post("/account/listenbrainz", data={"secret": SECRET, "csrf_token": tok})
    assert r.status_code == 303
    assert _link(sf, box, "alice", "listenbrainz") == ("lb-real-name", SECRET)
    with sf() as s:
        assert s.query(UserLink).count() == 1


def test_overlong_input_is_400_and_stores_nothing(
    client: TestClient, sf: sessionmaker[Session], box: SecretBox
) -> None:
    tok = _login(client, "alice")
    big = "x" * 5000
    r = client.post("/account/listenbrainz", data={"secret": big, "csrf_token": tok})
    assert r.status_code == 400 and "too long" in r.text and big not in r.text
    r = client.post("/account/navidrome", data={"username": big, "secret": "pw", "csrf_token": tok})
    assert r.status_code == 400 and "too long" in r.text
    assert _link(sf, box, "alice", "listenbrainz") is None
    assert _link(sf, box, "alice", "navidrome") is None


def test_navidrome_unreachable_message_and_password_not_stripped(
    client: TestClient, sf: sessionmaker[Session], box: SecretBox, fake: FakeValidator
) -> None:
    tok = _login(client, "alice")
    fake.ping = Ping.unreachable
    data = {"username": "ali", "secret": " pw ", "csrf_token": tok}
    r = client.post("/account/navidrome", data=data)
    assert (
        r.status_code == 400 and "reach Navidrome; try again." in r.text
    )  # the apostrophe is HTML-escaped
    assert "did not accept" not in r.text and _link(sf, box, "alice", "navidrome") is None
    fake.ping = Ping.ok
    client.post("/account/navidrome", data=data)
    assert _link(sf, box, "alice", "navidrome") == ("ali", " pw ")


def _subsonic(status: str, code: int = 0) -> httpx.Response:
    body: dict[str, object] = {"status": status}
    if code:
        body["error"] = {"code": code, "message": "x"}
    return httpx.Response(200, json={"subsonic-response": body})


def _ping(settings: Settings, sf: sessionmaker[Session], handler: object) -> DefaultValidator:
    t = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return DefaultValidator(
        polite_client("navidrome", settings, sf, transport=t),
        "http://nd",
        ListenBrainzClient(polite_client("listenbrainz", settings, sf, transport=t)),
    )


async def test_default_validator_ok_and_rejected(
    settings: Settings, sf: sessionmaker[Session]
) -> None:
    ok = _ping(settings, sf, lambda r: _subsonic("ok"))
    assert await ok.navidrome_ping("u", "p") == Ping.ok
    bad = _ping(settings, sf, lambda r: _subsonic("failed", 40))
    assert await bad.navidrome_ping("u", "p") == Ping.rejected


async def test_default_validator_transport_error_is_unreachable(
    settings: Settings, sf: sessionmaker[Session]
) -> None:
    def boom(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert await _ping(settings, sf, boom).navidrome_ping("u", "p") == Ping.unreachable


def test_listenbrainz_unreachable_message(
    client: TestClient, sf: sessionmaker[Session], box: SecretBox, fake: FakeValidator
) -> None:
    tok = _login(client, "alice")
    fake.lb_down = True
    r = client.post("/account/listenbrainz", data={"secret": SECRET, "csrf_token": tok})
    assert r.status_code == 400 and "reach ListenBrainz; try again." in r.text
    assert "did not accept" not in r.text and _link(sf, box, "alice", "listenbrainz") is None


def test_relinking_clears_needs_relink(
    client: TestClient, sf: sessionmaker[Session], box: SecretBox
) -> None:
    tok = _login(client, "alice")
    client.post("/account/listenbrainz", data={"secret": SECRET, "csrf_token": tok})
    new = SecretBox(Fernet.generate_key())
    client.app.state.secret_box = new  # type: ignore[attr-defined]
    assert "can no longer be decrypted" in client.get("/account").text
    client.post("/account/listenbrainz", data={"secret": SECRET, "csrf_token": tok})
    assert "can no longer be decrypted" not in client.get("/account").text
    with sf() as s:
        link = s.query(UserLink).one()
        assert not link.needs_relink and link_secret(new, link) == SECRET


async def test_default_validator_listenbrainz(
    settings: Settings, sf: sessionmaker[Session]
) -> None:
    def valid(r: httpx.Request) -> httpx.Response:
        assert r.headers["Authorization"] == "Token good"
        return httpx.Response(200, json={"valid": True, "user_name": "lbuser"})

    v = _ping(settings, sf, valid)
    assert await v.listenbrainz_username("good") == "lbuser"
    rejected = _ping(settings, sf, lambda r: httpx.Response(401, json={"code": 401}))
    assert await rejected.listenbrainz_username("bad") is None

    def boom(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(TransientError):
        await _ping(settings, sf, boom).listenbrainz_username("x")
