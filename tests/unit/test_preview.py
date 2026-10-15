import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devtools.preview.app import build_app, preview_env
from mixsync.web.templating import templates

THEMES = ("current", "demo")


@pytest.fixture
def preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    root = tmp_path / ".preview"
    for k, v in preview_env(root).items():  # build_app sets these; monkeypatch restores them
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(templates.env, "loader", templates.env.loader)  # restored after the test
    monkeypatch.setattr(templates.env, "cache", templates.env.cache)
    with TestClient(build_app(root), follow_redirects=False) as c:
        yield c


def _pages(c: TestClient) -> list[str]:
    html = c.get("/__preview").text
    return [p.replace("&amp;", "&") for p in re.findall(r'<li><a href="(/[^"]*)">', html)]


def _login(c: TestClient, user: str) -> None:
    assert c.post("/login", data={"username": user, "password": "preview"}).status_code == 303


def test_switcher_lists_themes_and_login_forms(preview: TestClient) -> None:
    html = preview.get("/__preview").text
    for t in THEMES:
        assert f"/__preview?theme={t}" in html
    for u in ("admin", "alice", "edge"):
        assert f"log in as {u}" in html


@pytest.mark.parametrize("user", ["admin", "alice", "edge"])
@pytest.mark.parametrize("theme", THEMES)
def test_every_seeded_page_renders(preview: TestClient, user: str, theme: str) -> None:
    pages = [p for p in _pages(preview) if not p.startswith("/__preview")]
    preview.get(f"/__preview?theme={theme}")  # sets the cookie
    _login(preview, user)
    seen = 0
    for path in pages:
        r = preview.get(path)
        if r.status_code == 403:  # another user's page, or one needing a role
            continue
        assert r.status_code == 200, (user, path, r.status_code)
        seen += 1
    assert seen >= 4


def test_demo_theme_changes_the_html(preview: TestClient) -> None:
    _login(preview, "alice")
    current = preview.get("/?theme=current").text
    demo = preview.get("/?theme=demo").text
    assert "demo.css" not in current and "demo.css" in demo
    assert preview.get("/themes/demo/static/demo.css").status_code == 200
    assert "demo.css" in preview.get("/").text  # the cookie keeps the theme


def test_logout_link_clears_the_session(preview: TestClient) -> None:
    _login(preview, "alice")
    assert preview.get("/").status_code == 200
    preview.get("/__preview/logout")
    assert preview.get("/").status_code == 303
