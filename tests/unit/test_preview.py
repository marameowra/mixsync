import re
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devtools.preview import app as preview_app
from devtools.preview.app import _reset, build_app, preview_env
from mixsync.web.templating import templates

THEMES = ("current", "demo", "swiss")


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


def _expected(user: str, label: str, path: str) -> int:
    """200 for a page the user owns or that has no owner; 403 for another user's page. An admin
    may also open any review detail (the approve capability)."""
    m = re.search(r"\((admin|alice|edge)\)", label)
    if m is None or m.group(1) == user:
        return 200
    return 200 if user == "admin" and path.startswith("/review/") else 403


@pytest.mark.parametrize("user", ["admin", "alice", "edge"])
@pytest.mark.parametrize("theme", THEMES)
def test_every_seeded_page_renders(preview: TestClient, user: str, theme: str) -> None:
    html = preview.get("/__preview").text
    pages = [
        (label, path.replace("&amp;", "&"))
        for path, label in re.findall(r'<li><a href="(/[^"]*)">([^<]*)</a>', html)
        if not path.startswith("/__preview")
    ]
    assert len(pages) >= 20
    preview.get(f"/__preview?theme={theme}")  # sets the cookie
    _login(preview, user)
    for label, path in pages:
        assert preview.get(path).status_code == _expected(user, label, path), (user, label)


def test_demo_theme_changes_the_html(preview: TestClient) -> None:
    _login(preview, "alice")
    current = preview.get("/?theme=current").text
    demo = preview.get("/?theme=demo").text
    assert "demo.css" not in current and "demo.css" in demo
    assert preview.get("/themes/demo/static/demo.css").status_code == 200
    assert "demo.css" in preview.get("/").text  # the cookie keeps the theme


def test_swiss_theme_serves_css_and_gates_nav_links(preview: TestClient) -> None:
    css = preview.get("/themes/swiss/static/theme.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    _login(preview, "admin")
    admin = preview.get("/?theme=swiss").text
    assert "/themes/swiss/static/theme.css" in admin and "/static/app.css" not in admin
    assert 'href="/migration"' in admin and "Waiting on you" in admin
    assert 'href="/" aria-current="page"' in admin
    assert 'href="/review" aria-current="page"' in preview.get("/review").text
    preview.get("/__preview/logout")
    _login(preview, "alice")
    assert 'href="/migration"' not in preview.get("/").text


def test_logout_link_clears_the_session(preview: TestClient) -> None:
    _login(preview, "alice")
    assert preview.get("/").status_code == 200
    preview.get("/__preview/logout")
    assert preview.get("/").status_code == 303


def test_unknown_theme_falls_back_to_current(preview: TestClient) -> None:
    _login(preview, "alice")
    r = preview.get("/?theme=../x")
    assert r.status_code == 200 and "demo.css" not in r.text and "theme" not in r.cookies
    preview.cookies.set("theme", "../x")
    r = preview.get("/")
    assert r.status_code == 200 and "demo.css" not in r.text


def test_reset_refuses_other_directories(tmp_path: Path) -> None:
    keep = tmp_path / "music"
    keep.mkdir()
    (keep / "song.flac").write_bytes(b"x")
    with pytest.raises(RuntimeError, match="not a .preview"):
        _reset(keep)
    assert (keep / "song.flac").read_bytes() == b"x"


def test_network_guard_refuses_non_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", socket.socket.connect)  # restored afterwards
    monkeypatch.setattr(socket, "create_connection", socket.create_connection)
    preview_app.block_network()
    with pytest.raises(RuntimeError, match="preview: network access"):
        socket.create_connection(("musicbrainz.org", 443))
    with socket.socket() as s, pytest.raises(RuntimeError, match="preview: network access"):
        s.connect(("93.184.216.34", 80))


def test_env_pins_external_services_to_a_dead_loopback(tmp_path: Path) -> None:
    env = preview_env(tmp_path)
    for k in ("MB_BASE_URL", "SLSKD_URL", "NAVIDROME_URL"):
        assert env[f"MIXSYNC_{k}"] == "http://127.0.0.1:9"
    for k in ("SLSKD_API_KEY", "ACOUSTID_APP_KEY", "ACOUSTID_USER_KEY", "NAVIDROME_PASSWORD"):
        assert env[f"MIXSYNC_{k}"] == ""
