import re
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mixsync.app import create_app
from mixsync.core.config import Settings
from mixsync.db.auth import create_user
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.migrate import upgrade
from mixsync.db.models.library import Track
from mixsync.library import tags

TAGS: dict[str, dict[str, str]] = {
    "a/1.flac": {"albumartist": "Band", "artist": "Band", "album": "LP", "title": "One",
                 "tracknumber": "1", "discnumber": "1", "date": "2020-05-01"},
    "a/2.flac": {"albumartist": "Band", "artist": "Guest", "album": "LP", "title": 'Two "q" <b>',
                 "tracknumber": "1", "discnumber": "2"},
    "b/1.flac": {"albumartist": "Other", "album": "EP", "title": "Solo", "tracknumber": "1"},
}  # fmt: skip
SUBMIT = "/library/unverified/submit"
Q = {"albumartist": "Band", "album": "LP"}


@pytest.fixture
def client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    upgrade()
    sf: sessionmaker[Session] = make_session_factory(make_engine(settings))
    with sf() as s, s.begin():
        create_user(s, "alice", "pw")
        create_user(s, "root", "pw", admin=True)
        for p in (*TAGS, "c/none.flac"):
            s.add(Track(path=p, status="unverified", batch_id="b", imported_at=datetime.now(UTC)))
        s.add(Track(path="v.flac", status="verified", batch_id="b", imported_at=datetime.now(UTC)))
    monkeypatch.setattr(tags, "read_fields", lambda p: TAGS[p.relative_to(p.parents[1]).as_posix()])
    monkeypatch.setattr(tags, "length_ms", lambda p: 61000 if p.name == "1.flac" else None)
    with TestClient(create_app(sf, Settings()), follow_redirects=False) as c:
        yield c


def _login(c: TestClient, user: str) -> None:
    c.cookies.clear()
    assert c.post("/login", data={"username": user, "password": "pw"}).status_code == 303


def _hidden(html: str) -> list[tuple[str, str]]:
    return re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', html)


def test_grouped_by_album_with_links(client: TestClient) -> None:
    _login(client, "root")
    html = client.get("/library/unverified").text
    assert "Unverified tracks (4)" in html
    assert "Band - LP" in html and "Other - EP" in html and "(no album tag)" in html
    assert html.count("Submit to MusicBrainz") == 2  # none for the untagged group
    assert (
        'submit?album=LP&amp;albumartist=Band"' in html
        or 'submit?albumartist=Band&amp;album=LP"' in html
    )


def test_seed_form_two_discs_and_track_artist(client: TestClient) -> None:
    _login(client, "root")
    html = client.get(SUBMIT, params=Q).text
    assert 'action="https://musicbrainz.org/release/add"' in html and 'target="_blank"' in html
    assert "onload" not in html and "submit()" not in html
    form = html[html.index('action="https://musicbrainz.org') :]
    assert "csrf_token" not in form  # never leak our token to MusicBrainz
    assert _hidden(form) == [
        ("name", "LP"),
        ("artist_credit.names.0.name", "Band"),
        ("events.0.date.year", "2020"),
        ("mediums.0.track.0.number", "1"),
        ("mediums.0.track.0.name", "One"),
        ("mediums.0.track.0.length", "61000"),
        ("mediums.1.track.0.number", "1"),
        ("mediums.1.track.0.name", "Two &#34;q&#34; &lt;b&gt;"),
        ("mediums.1.track.0.artist_credit.names.0.name", "Guest"),
        ("edit_note", "Seeded by MixSync from local files."),
    ]  # fmt: skip


def test_hidden_values_are_escaped(client: TestClient) -> None:
    _login(client, "root")
    html = client.get(SUBMIT, params=Q).text
    assert "<b>" not in html and 'Two "q"' not in html


def test_harmony_link_and_error(client: TestClient) -> None:
    _login(client, "root")
    ok = client.get(SUBMIT, params={**Q, "store_url": "https://x.bandcamp.com/album/a?b=1"}).text
    assert 'href="https://harmony.pulsewidth.org.uk/release?url=https%3A%2F%2Fx.bandcamp.com' in ok
    bad = client.get(SUBMIT, params={**Q, "store_url": "javascript:alert(1)"}).text
    assert 'role="alert"' in bad and "Open in Harmony" not in bad


def test_unknown_album_404(client: TestClient) -> None:
    _login(client, "root")
    assert client.get("/library/unverified/submit?album=Nope").status_code == 404


def test_requires_library_edit(client: TestClient) -> None:
    assert client.get(SUBMIT, params=Q).status_code in (303, 401, 403)  # anonymous
    _login(client, "alice")
    assert client.get(SUBMIT, params=Q).status_code == 403
    assert client.get("/library/unverified").status_code == 403
