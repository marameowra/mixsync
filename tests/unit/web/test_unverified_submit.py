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
                 "tracknumber": "1", "discnumber": "2", "date": "2020"},
    "b/1.flac": {"albumartist": "Other", "album": "EP", "title": "Solo", "tracknumber": "1"},
    "d/1.flac": {"albumartist": "Band", "album": "LP", "title": "Old", "date": "1999"},
    "g/1.flac": {"albumartist": "Band", "album": "LP", "title": "U"},
    "f/1.flac": {"albumartist": "Solo", "album": "S", "title": "Dated", "date": "2001",
                 "tracknumber": "01"},
    "f/2.flac": {"albumartist": "Solo", "album": "S", "title": "Undated", "tracknumber": "1"},
    "e/1.flac": {"albumartist": "Odd", "album": "Odd", "title": "Three", "tracknumber": "3"},
    "e/2.flac": {"albumartist": "Odd", "album": "Odd", "title": "One", "tracknumber": "1"},
    "e/3.flac": {"albumartist": "Odd", "album": "Odd"},
    "e/4.flac": {"albumartist": "Odd", "album": "Odd", "title": "Sq", "tracknumber": "1",
                 "discnumber": "\u00b2", "date": "\u00b2020"},
    "h/1.flac": {"albumartist": "Vinyl", "album": "V", "title": "Side A", "tracknumber": "A1"},
    "h/2.flac": {"albumartist": "Vinyl", "album": "V", "title": "Side B", "tracknumber": "B1"},
}  # fmt: skip
SUBMIT = "/library/unverified/submit"
Q = {"albumartist": "Band", "album": "LP", "year": "2020"}


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
    assert "Unverified tracks (14)" in html
    assert "Band - LP" in html and "Other - EP" in html and "(no album tag)" in html
    assert html.count("Submit to MusicBrainz") == 7  # none for the untagged group
    assert "Band - LP (2020)" in html and "Band - LP (1999)" in html  # same name, two groups
    assert "year=1999" in html and "year=2020" in html


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
        ("artist_credit.names.0.artist.name", "Band"),
        ("events.0.date.year", "2020"),
        ("mediums.0.track.0.number", "1"),
        ("mediums.0.track.0.name", "One"),
        ("mediums.0.track.0.length", "61000"),
        ("mediums.1.track.0.number", "1"),
        ("mediums.1.track.0.name", "Two &#34;q&#34; &lt;b&gt;"),
        ("mediums.1.track.0.artist_credit.names.0.name", "Guest"),
        ("mediums.1.track.0.artist_credit.names.0.artist.name", "Guest"),
        ("edit_note", "Seeded by MixSync from local files."),
    ]  # fmt: skip


def test_order_fallbacks_and_non_ascii_digits(client: TestClient) -> None:
    _login(client, "root")
    r = client.get(SUBMIT, params={"albumartist": "Odd", "album": "Odd"})
    assert r.status_code == 200  # "\u00b2" in disc/date must not 500
    f = _hidden(r.text[r.text.index('action="https://musicbrainz.org') :])
    tracks = [(k, v) for k, v in f if k.endswith((".number", ".name")) and ".track." in k]
    assert tracks == [  # out of order on disk -> sorted; missing number follows the max
        ("mediums.0.track.0.number", "1"),
        ("mediums.0.track.0.name", "One"),
        ("mediums.0.track.1.number", "1"),
        ("mediums.0.track.1.name", "Sq"),  # disc "\u00b2" falls back to disc 1
        ("mediums.0.track.2.number", "3"),
        ("mediums.0.track.2.name", "Three"),
        ("mediums.0.track.3.number", "4"),
        ("mediums.0.track.3.name", "3"),  # no title: file stem
    ]
    assert not any(k == "events.0.date.year" for k, _ in f)  # "\u00b2020" is not a year
    assert "disc 1 track 1" in r.text  # duplicate warning


def test_undated_group_merges_into_single_dated_match(client: TestClient) -> None:
    _login(client, "root")
    html = client.get("/library/unverified").text
    assert "Solo - S (2001)" in html and "Solo - S</h2>" not in html
    # Band/LP has two dated groups, so its undated track stays separate
    assert "Band - LP</h2>" in html


def test_duplicate_warning_compares_parsed_numbers(client: TestClient) -> None:
    _login(client, "root")
    page = client.get(SUBMIT, params={"albumartist": "Solo", "album": "S", "year": "2001"}).text
    assert "disc 1 track 1" in page  # "01" and "1"


def test_no_duplicate_warning_for_clean_album(client: TestClient) -> None:
    _login(client, "root")
    assert "same number" not in client.get(SUBMIT, params=Q).text
    vinyl = client.get(SUBMIT, params={"albumartist": "Vinyl", "album": "V"}).text
    assert "same number" not in vinyl  # "A1" and "B1" don't both count as track 0


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


def test_seed_page_sets_no_referrer(client: TestClient) -> None:
    _login(client, "root")
    r = client.get(SUBMIT, params=Q)
    assert 'rel="noreferrer"' in r.text and r.headers["referrer-policy"] == "no-referrer"


def test_unknown_album_404(client: TestClient) -> None:
    _login(client, "root")
    assert client.get("/library/unverified/submit?album=Nope").status_code == 404


def test_requires_library_edit(client: TestClient) -> None:
    assert client.get(SUBMIT, params=Q).status_code in (303, 401, 403)  # anonymous
    _login(client, "alice")
    assert client.get(SUBMIT, params=Q).status_code == 403
    assert client.get("/library/unverified").status_code == 403
