import pytest

from mixsync.sources.query import queries


@pytest.mark.parametrize(
    ("artist", "album", "expected"),
    [
        ("Radiohead", "OK Computer", ["Radiohead OK Computer", "OK Computer"]),
        ("Sigur Rós", "Ágætis byrjun", ["Sigur Rós Ágætis byrjun", "Ágætis byrjun"]),
        ("Björk", "Homogenic", ["Björk Homogenic", "Homogenic"]),
        ("坂本龍一", "音楽図鑑", ["坂本龍一 音楽図鑑", "音楽図鑑"]),
        (
            "AC/DC",
            "Back in Black",
            ["AC/DC Back in Black", "AC DC Back in Black", "Back in Black"],
        ),
        (
            "Jay-Z feat. Alicia Keys",
            "Empire State",
            [
                "Jay-Z feat. Alicia Keys Empire State",
                "Jay-Z Empire State",
                "Jay Z Empire State",
                "Empire State",
            ],
        ),
        (
            "Artist",
            "Album (feat. Someone) [Deluxe]",
            [
                "Artist Album (feat. Someone) [Deluxe]",
                "Artist Album [Deluxe]",
                "Artist Album Deluxe",
                "Album Deluxe",
            ],
        ),
        ("Artist", "Album ft. Guest", ["Artist Album ft. Guest", "Artist Album", "Album"]),
        ("  Artist ", "Album  ", ["Artist Album", "Album"]),
    ],
)
def test_queries(artist: str, album: str, expected: list[str]) -> None:
    assert queries(artist, album) == expected


def test_all_punctuation_album_has_no_empty_query() -> None:
    assert "" not in queries("Artist", "!!!")
