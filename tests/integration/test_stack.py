"""request -> fake slskd -> download -> verify -> import or review -> Navidrome rescan, against the
compose stack (compose.yml). Run with tests/integration/run.sh."""

import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tests.integration.conftest import LIBRARY, WEB, csrf, request_album, wait_for
from tests.integration.fakes.server import ALBUMS, ARTIST

pytestmark = pytest.mark.integration

HAPPY, REVIEW = ALBUMS


def test_login_page_and_bad_password() -> None:
    page = httpx.get(f"{WEB}/login")
    assert page.status_code == 200
    assert 'autocomplete="username"' in page.text
    assert 'autocomplete="current-password"' in page.text
    bad = httpx.post(f"{WEB}/login", data={"username": "admin", "password": "wrong"})
    assert bad.status_code == 401


def test_happy_path_lands_in_library_and_navidrome(
    web: httpx.Client, navidrome: Callable[..., dict[str, Any]]
) -> None:
    request_album(web, HAPPY["id"])
    listing = web.get("/requests").text  # the row names the release, or its id until resolved
    assert HAPPY["title"] in listing or HAPPY["id"][:8] in listing

    folder = LIBRARY / ARTIST / f"{HAPPY['title']} (2020)"
    files = wait_for(
        "the album in the library",
        lambda: names if len(names := sorted(p.name for p in folder.glob("*.flac"))) == 3 else None,
    )
    assert files == [f"01-0{i} {t['title']}.flac" for i, t in enumerate(HAPPY["tracks"], 1)]

    def listed() -> dict[str, Any] | None:
        albums = navidrome("getAlbumList2", type="newest")["albumList2"].get("album", [])
        return next((a for a in albums if a["name"] == HAPPY["title"]), None)

    album = wait_for("Navidrome to list the album after mixsync's rescan", listed, 180)
    assert (album["artist"], album["songCount"]) == (ARTIST, 3)


def test_review_band_lands_in_review_not_library(web: httpx.Client) -> None:
    request_album(web, REVIEW["id"])

    page = wait_for(
        "the album in review",
        lambda: r.text if REVIEW["title"] in (r := web.get("/review")).text else None,
    )
    match = re.search(r'href="/review/(\d+)"', page)
    assert match
    detail = web.get(f"/review/{match.group(1)}")
    assert detail.status_code == 200
    assert "looks transcoded" in detail.text  # the veto the fake's low-passed files trip
    assert "Needs review" in web.get("/requests").text
    assert not (LIBRARY / ARTIST / f"{REVIEW['title']} (2020)").exists()


def test_review_page_renders_with_its_form(web: httpx.Client) -> None:
    page = web.get("/review")
    assert page.status_code == 200
    csrf(page.text)  # its forms post with the session's token
