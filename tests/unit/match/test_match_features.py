import pytest

from mixsync.match.features import string_dist


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Some String", "Some String"),
        ("Some String", "Some.String!"),
        ("Some String", "sOME sTring"),
        ("My Song (EP)", "My Song"),
        ("The Song Title", "Song Title, The"),
        ("A Song Title", "Song Title, A"),
        ("An Album Title", "Album Title, An"),
        ("", ""),
        ("Untitled", "[Untitled]"),
        ("And", "&"),
        ("\xe9\xe1\xf1", "ean"),
    ],
)
def test_matching_distance(a: str, b: str) -> None:
    assert string_dist(a, b) == 0.0


def test_different_distance() -> None:
    assert string_dist("Some String", "Totally Different") != 0.0


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Draft Beer", "Draft Whiskey"),
        ("Left Field", "Left Symphony"),
        ("Gift Ideas", "Gift Cards"),
        ("Craft Beer", "Craft Wine"),
    ],
)
def test_featuring_pattern_does_not_match_mid_word(a: str, b: str) -> None:
    assert string_dist(a, b) > 0.3


@pytest.mark.parametrize(
    ("worse", "better", "reference"),
    [
        ("XXX Band Name", "The Band Name", "Band Name"),
        ("One .Two.", "One (Two)", "One"),
        ("One .Two.", "One [Two]", "One"),
        ("My Song blah Someone", "My Song feat Someone", "My Song"),
    ],
)
def test_relative_weights(worse: str, better: str, reference: str) -> None:
    assert string_dist(better, reference) < string_dist(worse, reference)


def test_solo_pattern_does_not_crash() -> None:
    string_dist("The ", "")
    string_dist("(EP)", "(EP)")
    string_dist(", An", "")


def test_none_handling() -> None:
    assert string_dist(None, None) == 0.0
    assert string_dist("x", None) == 1.0


def test_non_latin_strings_are_not_equal() -> None:
    assert string_dist("Кино", "Алиса") > 0.5
    assert string_dist("東京", "大阪") == 1.0
    assert string_dist("Кино", "КИНО") == 0.0
