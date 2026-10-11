from urllib.parse import parse_qs, urlsplit

import pytest

from mixsync.submit.harmony import harmony_url
from mixsync.submit.mb_seed import (
    EDIT_NOTE,
    Credit,
    SeedMedium,
    SeedRelease,
    SeedTrack,
    build_seed,
)

A = [Credit("Artist")]


def test_single_disc_full_snapshot():
    r = SeedRelease(
        "Album",
        A,
        [SeedMedium([SeedTrack("1", "One", 61000), SeedTrack("2", "Two", 5000)], "CD")],
        types=["Album"],
        status="Official",
        year=2020,
        month=3,
        day=9,
        label="Lbl",
        catalog_number="C-1",
        barcode="123",
    )
    assert build_seed(r) == [
        ("name", "Album"),
        ("artist_credit.names.0.name", "Artist"),
        ("type", "Album"),
        ("status", "Official"),
        ("events.0.date.year", "2020"),
        ("events.0.date.month", "3"),
        ("events.0.date.day", "9"),
        ("labels.0.name", "Lbl"),
        ("labels.0.catalog_number", "C-1"),
        ("barcode", "123"),
        ("mediums.0.format", "CD"),
        ("mediums.0.track.0.number", "1"),
        ("mediums.0.track.0.name", "One"),
        ("mediums.0.track.0.length", "61000"),
        ("mediums.0.track.1.number", "2"),
        ("mediums.0.track.1.name", "Two"),
        ("mediums.0.track.1.length", "5000"),
        ("edit_note", EDIT_NOTE),
    ]


def test_minimal_omits_unknowns_and_missing_durations():
    out = build_seed(SeedRelease("A", A, [SeedMedium([SeedTrack("1", "T")])]))
    assert out == [
        ("name", "A"),
        ("artist_credit.names.0.name", "Artist"),
        ("mediums.0.track.0.number", "1"),
        ("mediums.0.track.0.name", "T"),
        ("edit_note", "Seeded by MixSync from local files."),
    ]


def test_multi_disc_indexes():
    m = [SeedMedium([SeedTrack("1", "a")]), SeedMedium([SeedTrack("1", "b")])]
    assert ("mediums.1.track.0.name", "b") in build_seed(SeedRelease("A", A, m))


def test_unicode_passthrough():
    out = dict(build_seed(SeedRelease("Ünï — 日本語", [Credit("Björk")], [SeedMedium([])])))
    assert out["name"] == "Ünï — 日本語"
    assert out["artist_credit.names.0.name"] == "Björk"


def test_various_artists_and_track_credits_with_join_phrase():
    t = SeedTrack("1", "Duet", artists=[Credit("X", " feat. "), Credit("Y")])
    r = SeedRelease("Comp", [Credit("Various Artists")], [SeedMedium([t, SeedTrack("2", "Solo")])])
    out = build_seed(r)
    assert ("mediums.0.track.0.artist_credit.names.0.name", "X") in out
    assert ("mediums.0.track.0.artist_credit.names.0.join_phrase", " feat. ") in out
    assert ("mediums.0.track.0.artist_credit.names.1.name", "Y") in out
    assert not any(k.startswith("mediums.0.track.1.artist_credit") for k, _ in out)


def test_edit_note_is_last():
    assert build_seed(SeedRelease("A", A, []))[-1] == ("edit_note", EDIT_NOTE)


def test_harmony_url_encodes():
    store = "https://x.bandcamp.com/album/a b?x=1&y=2"
    u = urlsplit(harmony_url(store))
    assert u.netloc == "harmony.pulsewidth.org.uk" and u.path == "/release"
    assert parse_qs(u.query) == {"url": [store]}
    assert "&y=2" not in u.query


@pytest.mark.parametrize("bad", ["", "ftp://x/y", "javascript:alert(1)", "example.com", "http://"])
def test_harmony_url_rejects(bad):
    with pytest.raises(ValueError):
        harmony_url(bad)
