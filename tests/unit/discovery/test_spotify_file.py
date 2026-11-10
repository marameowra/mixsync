"""Parsing Spotify playlist files: data-export JSON and Exportify CSV."""

import json

import pytest

from mixsync.discovery.mirrored import UpstreamTrack
from mixsync.discovery.spotify_file import BadFile, parse


def export(*lists: tuple[str, list[object]]) -> bytes:
    return json.dumps({"playlists": [{"name": n, "items": i} for n, i in lists]}).encode()


def item(artist: str, title: str) -> dict[str, object]:
    return {"track": {"trackName": title, "artistName": artist, "albumName": "Al", "trackUri": "u"}}


def test_data_export_json_keeps_order_and_skips_local_files_and_episodes() -> None:
    data = export(
        (
            "One",
            [item("A", "x"), {"track": None, "localTrack": {}}, {"episode": {}}, item("B", "y")],
        ),
        ("Two", [item("C", "z")]),
    )
    assert parse(data, "Playlist1.json") == {
        "One": [UpstreamTrack("A", "x"), UpstreamTrack("B", "y")],
        "Two": [UpstreamTrack("C", "z")],
    }


def test_exportify_csv_uses_isrc_first_artist_and_the_file_name() -> None:
    csv_text = (
        "Track URI,Track Name,Artist Name(s),ISRC\n"
        'u1,Song,"Lead;Guest",gbabc1234567\n'
        "u2,Other,Solo,\n"
    )
    assert parse(csv_text.encode("utf-8-sig"), "Road Trip.csv") == {
        "Road Trip": [UpstreamTrack("Lead", "Song", "GBABC1234567"), UpstreamTrack("Solo", "Other")]
    }


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"{not json",
        b"[1, 2]",
        b'{"playlists": []}',
        b'{"playlists": [{"name": "x"}]}',
        b'{"playlists": [{"items": []}]}',
        b"Name,Artist\nx,y\n",
        b"\xff\xfe\x00",
        b"x" * 5_000_001,
    ],
)
def test_malformed_files_are_rejected(data: bytes) -> None:
    with pytest.raises(BadFile):
        parse(data, "f.json")
