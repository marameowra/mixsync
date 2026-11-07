from collections.abc import Iterator

import pytest
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from jinja2 import BaseLoader, ChoiceLoader, DictLoader
from sqlalchemy.orm import Session, sessionmaker

from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.web.deps import CurrentUser
from mixsync.web.templating import render, templates

FRAGMENT = (
    '{% import "macros/live.html" as live %}'
    "{% call live.region('/t/live', busy) %}state=x{% endcall %}"
)
PAGE = '{% extends "base.html" %}{% block content %}{% include "t_fragment.html" %}{% endblock %}'
HX = {"HX-Request": "true"}


@pytest.fixture
def client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    upgrade()
    sf: sessionmaker[Session] = make_session_factory(make_engine(settings))
    with sf() as s, s.begin():
        create_user(s, "alice", "pw")
    loader: BaseLoader = templates.env.loader  # pyright: ignore[reportAssignmentType]
    test_loader = DictLoader({"t_fragment.html": FRAGMENT, "t_page.html": PAGE})
    monkeypatch.setattr(templates.env, "loader", ChoiceLoader([test_loader, loader]))
    router = APIRouter()

    @router.get("/t/live")
    def live(request: Request, user: CurrentUser, busy: bool = True) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]  # registered by decorator
        return render(request, "t_page.html", "t_fragment.html", {"busy": busy, "user": user})

    app = create_app(sf)
    app.include_router(router)  # test-only: never part of the production routing
    with TestClient(app, follow_redirects=False) as c:
        yield c


def login(client: TestClient) -> None:
    assert client.post("/login", data={"username": "alice", "password": "pw"}).status_code == 303


def test_in_progress_region_polls_and_settled_does_not(client: TestClient) -> None:
    login(client)
    busy = client.get("/t/live", headers=HX).text
    assert 'hx-get="/t/live"' in busy
    assert 'hx-trigger="every 5s [!document.hidden]"' in busy
    assert 'hx-swap="outerHTML"' in busy
    settled = client.get("/t/live?busy=false", headers=HX).text
    assert "state=x" in settled
    assert "hx-" not in settled


def test_htmx_request_gets_fragment_and_plain_request_gets_full_page(client: TestClient) -> None:
    login(client)
    fragment = client.get("/t/live", headers=HX).text
    assert "<html" not in fragment and "state=x" in fragment
    page = client.get("/t/live").text
    assert "<html" in page and "state=x" in page


def test_unauthenticated_htmx_request_is_refused_like_a_full_one(client: TestClient) -> None:
    full = client.get("/t/live")
    htmx = client.get("/t/live", headers=HX)
    assert full.status_code == htmx.status_code == 303
    assert full.headers["location"] == htmx.headers["location"] == "/login"


def test_base_template_loads_vendored_htmx_from_static(client: TestClient) -> None:
    html = client.get("/login").text
    assert 'src="/static/htmx-2.0.4.min.js"' in html
    assert client.get("/static/htmx-2.0.4.min.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200
