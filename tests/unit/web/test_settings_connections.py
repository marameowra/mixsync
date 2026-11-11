"""Test-connection buttons on /settings (#348): one request per click, through PoliteClient,
using the form's values, saving nothing. Fakes only, no real network."""

import asyncio
import re
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.auth import AppSettings
from mixsync.db.models.infra import RateBucket
from mixsync.web.routes import connections

Handler = Callable[[httpx.Request], httpx.Response]
SENT: list[httpx.Request] = []

# per service: the form it submits, a working answer and a rejected-credentials answer
FORMS = {
    "slskd": {"slskd_url": "http://slskd.test:5030", "slskd_api_key": "k"},
    "navidrome": {
        "navidrome_url": "http://nd.test:4533",
        "navidrome_user": "admin",
        "navidrome_password": "pw",
    },
    "musicbrainz": {"mb_base_url": "http://mb.test"},
    "acoustid": {"acoustid_app_key": "k"},
}


def _subsonic(status: str, **extra: object) -> httpx.Response:
    return httpx.Response(200, json={"subsonic-response": {"status": status, **extra}})


OK: dict[str, Handler] = {
    "slskd": lambda r: httpx.Response(200, json={"version": {}}),
    "navidrome": lambda r: _subsonic("ok"),
    "musicbrainz": lambda r: httpx.Response(200, json={"id": "x", "name": "United Kingdom"}),
    "acoustid": lambda r: httpx.Response(200, json={"status": "ok", "results": []}),
}
REJECTED: dict[str, Handler] = {
    "slskd": lambda r: httpx.Response(401),
    "navidrome": lambda r: _subsonic("failed", error={"code": 40, "message": "Wrong user"}),
    "acoustid": lambda r: httpx.Response(
        400, json={"status": "error", "error": {"code": 4, "message": "invalid API key"}}
    ),
}


@pytest.fixture
def sf(settings: Settings) -> sessionmaker[Session]:
    upgrade()
    sf = make_session_factory(make_engine(settings))
    with sf() as s, s.begin():
        create_user(s, "alice", "pw")
        create_user(s, "root", "pw", admin=True)
    return sf


@pytest.fixture
def client(sf: sessionmaker[Session]) -> Iterator[TestClient]:
    with TestClient(create_app(sf, Settings()), follow_redirects=False) as c:
        yield c


def login(c: TestClient, user: str) -> str:
    assert c.post("/login", data={"username": user, "password": "pw"}).status_code == 303
    m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', c.get("/account").text)
    assert m
    return m.group(1)


def fake(c: TestClient, handler: Handler) -> None:
    SENT.clear()

    def record(request: httpx.Request) -> httpx.Response:
        SENT.append(request)
        return handler(request)

    c.app.state.connection_transport = httpx.MockTransport(record)  # type: ignore[attr-defined]


def press(c: TestClient, tok: str, service: str, **form: str) -> str:
    r = c.post(f"/settings/test/{service}", data={"csrf_token": tok, **form})
    assert r.status_code == 200
    return r.text


@pytest.mark.parametrize("service", list(FORMS))
def test_working_service_reports_ok_and_saves_nothing(
    client: TestClient, sf: sessionmaker[Session], service: str
) -> None:
    tok = login(client, "root")
    fake(client, OK[service])
    assert "OK" in press(client, tok, service, **FORMS[service])
    assert len(SENT) == 1  # one lightweight request per click
    assert SENT[0].headers["User-Agent"].startswith("MixSync/")  # via PoliteClient
    with sf() as s:
        assert s.get(AppSettings, 1) is None  # nothing persisted, not even a blank row


@pytest.mark.parametrize("service", list(REJECTED))
def test_rejected_credentials_report_auth_failed(client: TestClient, service: str) -> None:
    tok = login(client, "root")
    fake(client, REJECTED[service])
    assert "Authentication failed" in press(client, tok, service, **FORMS[service])


@pytest.mark.parametrize("service", list(FORMS))
def test_unreachable_and_timeout_are_told_apart(client: TestClient, service: str) -> None:
    tok = login(client, "root")

    def refuse(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=r)

    def hang(r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=r)

    fake(client, refuse)
    assert "Unreachable" in press(client, tok, service, **FORMS[service])
    fake(client, hang)
    assert "Timed out." in press(client, tok, service, **FORMS[service])


@pytest.mark.parametrize("service", ["slskd", "navidrome", "musicbrainz"])
def test_bad_url_is_refused_without_a_request(client: TestClient, service: str) -> None:
    tok = login(client, "root")
    fake(client, OK[service])
    form = {k: "ftp://x" if k.endswith("_url") else v for k, v in FORMS[service].items()}
    assert "Bad URL" in press(client, tok, service, **form)
    assert SENT == []


def test_wrong_base_url_reports_the_status(client: TestClient) -> None:
    tok = login(client, "root")
    fake(client, lambda r: httpx.Response(404))
    assert "returned 404" in press(client, tok, "musicbrainz", mb_base_url="http://mb.test")


def test_form_values_are_used_and_blank_secret_uses_the_saved_one(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    tok = login(client, "root")
    r = client.post("/settings", data={"csrf_token": tok, "slskd_api_key": "saved"})
    assert r.status_code == 303
    fake(client, OK["slskd"])
    assert "OK" in press(client, tok, "slskd", slskd_url="http://unsaved.test:1", slskd_api_key="")
    assert SENT[0].url.host == "unsaved.test"
    assert SENT[0].headers["X-API-Key"] == "saved"
    with sf() as s:
        assert s.scalars(select(AppSettings.slskd_url)).one() is None  # the URL wasn't saved


@pytest.mark.parametrize("service", ["musicbrainz", "acoustid"])
def test_requests_spend_the_shared_rate_limit(
    client: TestClient, sf: sessionmaker[Session], service: str
) -> None:
    tok = login(client, "root")
    fake(client, OK[service])
    press(client, tok, service, **FORMS[service])
    with sf() as s:
        row = s.get(RateBucket, service)
        assert row is not None and row.tokens < row.capacity


def test_missing_credentials_are_named(client: TestClient) -> None:
    tok = login(client, "root")
    fake(client, OK["navidrome"])
    text = press(client, tok, "navidrome", navidrome_url="http://nd.test")
    assert "Set navidrome_user and navidrome_password first." in text
    assert SENT == []


def test_non_admin_is_refused(client: TestClient) -> None:
    tok = login(client, "alice")
    fake(client, OK["slskd"])
    r = client.post("/settings/test/slskd", data={"csrf_token": tok, **FORMS["slskd"]})
    assert r.status_code == 403
    assert SENT == []


def test_unknown_service_is_404(client: TestClient) -> None:
    tok = login(client, "root")
    assert client.post("/settings/test/discogs", data={"csrf_token": tok}).status_code == 404


def test_settings_page_has_a_test_button_per_service(client: TestClient) -> None:
    login(client, "root")
    page = client.get("/settings").text
    for service in FORMS:
        assert f'hx-post="/settings/test/{service}"' in page
        assert f'id="test-{service}"' in page
    assert "onclick" not in page and "hx-on" not in page


def test_a_non_json_answer_is_a_visible_error(client: TestClient) -> None:
    tok = login(client, "root")
    fake(client, lambda r: httpx.Response(200, text="<html>"))
    assert "the answer was not JSON" in press(client, tok, "slskd", **FORMS["slskd"])


@pytest.mark.parametrize("service", ["slskd", "navidrome", "musicbrainz"])
def test_a_server_error_is_not_called_unreachable(client: TestClient, service: str) -> None:
    tok = login(client, "root")
    fake(client, lambda r: httpx.Response(500))
    text = press(client, tok, service, **FORMS[service])
    assert "Failed:" in text and "returned 500" in text
    assert "Unreachable" not in text


def test_the_whole_click_times_out(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    tok = login(client, "root")
    monkeypatch.setattr(connections, "TIMEOUT", 0.05)

    async def stall(r: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    client.app.state.connection_transport = httpx.MockTransport(stall)  # type: ignore[attr-defined]
    assert "Timed out." in press(client, tok, "slskd", **FORMS["slskd"])


# Prowlarr, qBittorrent and SABnzbd (#414). qBittorrent logs in first, so it sends two requests.
SOURCES = {
    "prowlarr": {"prowlarr_url": "http://pr.test:9696", "prowlarr_api_key": "PRKEY"},
    "qbittorrent": {
        "qbittorrent_url": "http://qb.test:8080",
        "qbittorrent_username": "admin",
        "qbittorrent_password": "QBPASS",
    },
    "sabnzbd": {"sabnzbd_url": "http://sab.test:8085", "sabnzbd_api_key": "SABKEY"},
}


def _qb(good: bool) -> Handler:
    def handle(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok." if good else "Fails.")
        return httpx.Response(200, text="v5.0.0")

    return handle


SOURCE_OK: dict[str, Handler] = {
    "prowlarr": lambda r: httpx.Response(200, json={"version": "1"}),
    "qbittorrent": _qb(True),
    "sabnzbd": lambda r: httpx.Response(200, json={"queue": {"slots": []}}),
}
SOURCE_REJECTED: dict[str, Handler] = {
    "prowlarr": lambda r: httpx.Response(401),
    "qbittorrent": _qb(False),
    "sabnzbd": lambda r: httpx.Response(200, json={"status": False, "error": "API Key Incorrect"}),
}


@pytest.mark.parametrize("service", list(SOURCES))
def test_source_ok_and_saves_nothing(
    client: TestClient, sf: sessionmaker[Session], service: str
) -> None:
    tok = login(client, "root")
    fake(client, SOURCE_OK[service])
    text = press(client, tok, service, **SOURCES[service])
    assert "OK" in text
    assert all(r.headers["User-Agent"].startswith("MixSync/") for r in SENT)
    assert not any(s in text for s in ("PRKEY", "QBPASS", "SABKEY"))
    with sf() as s:
        assert s.get(AppSettings, 1) is None


@pytest.mark.parametrize(
    ("service", "answer"),
    [
        ("prowlarr", httpx.Response(200, text="<html>")),
        ("sabnzbd", httpx.Response(200, text="<html>")),
        ("sabnzbd", httpx.Response(200, json={"version": "4"})),
    ],
)
def test_a_200_from_something_else_is_not_ok(
    client: TestClient, service: str, answer: httpx.Response
) -> None:
    tok = login(client, "root")
    fake(client, lambda r: answer)
    text = press(client, tok, service, **SOURCES[service])
    assert "Failed:" in text and "OK" not in text


@pytest.mark.parametrize("service", list(SOURCES))
def test_source_rejected_credentials_report_auth_failed(client: TestClient, service: str) -> None:
    tok = login(client, "root")
    fake(client, SOURCE_REJECTED[service])
    text = press(client, tok, service, **SOURCES[service])
    assert "Authentication failed" in text
    assert not any(s in text for s in ("PRKEY", "QBPASS", "SABKEY"))


@pytest.mark.parametrize("service", list(SOURCES))
def test_source_bad_url_and_missing_credential_send_nothing(
    client: TestClient, service: str
) -> None:
    tok = login(client, "root")
    fake(client, SOURCE_OK[service])
    form = {k: "ftp://x" if k.endswith("_url") else v for k, v in SOURCES[service].items()}
    assert "Bad URL" in press(client, tok, service, **form)
    secret = next(k for k in SOURCES[service] if k.endswith(("_key", "_password")))
    form = {k: "" if k == secret else v for k, v in SOURCES[service].items()}
    assert f"Set {secret}" in press(client, tok, service, **form)
    assert SENT == []


def test_qbittorrent_ping_only_logs_in_and_reads_the_version(client: TestClient) -> None:
    tok = login(client, "root")
    fake(client, SOURCE_OK["qbittorrent"])
    press(client, tok, "qbittorrent", **SOURCES["qbittorrent"])
    assert [r.url.path for r in SENT] == ["/api/v2/auth/login", "/api/v2/app/version"]


def test_sabnzbd_ping_checks_the_key_and_keeps_it_out_of_the_url(client: TestClient) -> None:
    tok = login(client, "root")
    fake(client, SOURCE_OK["sabnzbd"])
    press(client, tok, "sabnzbd", **SOURCES["sabnzbd"])
    assert b"mode=queue" in SENT[0].content and b"apikey=SABKEY" in SENT[0].content
    assert "SABKEY" not in str(SENT[0].url)


def test_source_form_values_beat_saved_and_blank_uses_saved(
    client: TestClient, sf: sessionmaker[Session]
) -> None:
    tok = login(client, "root")
    client.post("/settings", data={"csrf_token": tok, "prowlarr_api_key": "saved"})
    fake(client, SOURCE_OK["prowlarr"])
    press(client, tok, "prowlarr", prowlarr_url="http://unsaved.test", prowlarr_api_key="")
    assert SENT[0].url.host == "unsaved.test" and SENT[0].headers["X-Api-Key"] == "saved"
    with sf() as s:
        assert s.scalars(select(AppSettings.prowlarr_url)).one() is None


def test_settings_page_has_source_test_buttons(client: TestClient) -> None:
    login(client, "root")
    page = client.get("/settings").text
    for service in SOURCES:
        assert f'hx-post="/settings/test/{service}"' in page
