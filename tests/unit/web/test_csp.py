from collections.abc import Iterator

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.web.csp import CSP, csp_with_form_target

HX = {"HX-Request": "true"}


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    upgrade()
    sf: sessionmaker[Session] = make_session_factory(make_engine(settings))
    with sf() as s, s.begin():
        create_user(s, "alice", "pw")
    router = APIRouter()

    @router.get("/t/boom")
    def boom() -> None:  # pyright: ignore[reportUnusedFunction]  # registered by decorator
        raise RuntimeError("boom")

    app = create_app(sf)
    app.include_router(router)  # test-only: never part of the production routing
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as c:
        yield c


def test_csp_on_every_kind_of_response(client: TestClient) -> None:
    responses = {
        "login page": client.get("/login"),
        "static file": client.get("/static/app.css"),
        "404": client.get("/no-such-page"),
        "fragment 404": client.get("/no-such-page", headers=HX),
        "redirect": client.get("/"),
        "json route": client.get("/healthz"),
        "500": client.get("/t/boom"),
    }
    assert client.post("/login", data={"username": "alice", "password": "pw"}).status_code == 303
    responses["logged-in page"] = client.get("/requests")
    responses["live fragment"] = client.get("/requests", headers=HX)
    codes = [r.status_code for r in responses.values()]
    assert codes == [200, 200, 404, 404, 303, 200, 500, 200, 200]
    assert "<html" not in responses["live fragment"].text
    for kind, r in responses.items():
        assert r.headers.get("content-security-policy") == CSP, kind


def test_form_target_policy_only_adds_the_one_origin() -> None:
    assert csp_with_form_target("https://musicbrainz.org/release/add") == CSP.replace(
        "form-action 'self'", "form-action 'self' https://musicbrainz.org"
    )
