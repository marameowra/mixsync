import unicodedata

from hypothesis import given
from hypothesis import strategies as st

from mixsync.core.config import Settings
from mixsync.library.paths import SEGMENT_MAX_BYTES, render, sanitize, unique

TEMPLATE = Settings().path_template
ILLEGAL = set('<>:"/\\|?*')
text = st.text(max_size=300)


def _no_illegal(s: str) -> bool:
    return not any(c in ILLEGAL or unicodedata.category(c) == "Cc" for c in s)


def r(**kw: object) -> str:
    args: dict[str, object] = dict(
        albumartist="A", album="B", year=2000, disc=1, track=2, title="T", ext="flac"
    )
    return render(TEMPLATE, **{**args, **kw})  # pyright: ignore[reportArgumentType]


def test_default_template() -> None:
    assert r(albumartist="Radiohead", album="Kid A", title="Idioteque", track=8) == (
        "Radiohead/Kid A (2000)/01-08 Idioteque.flac"
    )


def test_missing_year_is_omitted_cleanly() -> None:
    assert r(year=None) == "A/B/01-02 T.flac"


def test_awkward_values() -> None:
    assert r(title="AC/DC: Who?", album="CON", albumartist="..") == (
        "_/_CON (2000)/01-02 ACDC Who.flac"
    )
    assert sanitize("NUL.txt") == "_NUL.txt"


@given(text)
def test_sanitize_properties(s: str) -> None:
    out = sanitize(s)
    assert out == sanitize(s)
    assert out and _no_illegal(out)
    assert out == out.rstrip(" .") or out == "_"
    assert len(out.encode()) <= SEGMENT_MAX_BYTES + 1  # "_" prefix for reserved names
    assert unicodedata.is_normalized("NFC", out)
    assert out.upper() not in {"CON", "PRN", "AUX", "NUL", "COM1", "LPT9"}


@given(text, text, text, st.none() | st.integers(0, 9999), st.integers(0, 99), st.integers(0, 999))
def test_render_properties(
    artist: str, album: str, title: str, year: int | None, d: int, t: int
) -> None:
    kw = dict(albumartist=artist, album=album, title=title, year=year, disc=d, track=t)
    out = r(**kw)
    assert out == r(**kw)
    segments = out.split("/")
    assert len(segments) == 3 and out.endswith(".flac")
    assert all(0 < len(s.encode()) <= SEGMENT_MAX_BYTES + 1 for s in segments)
    assert all(_no_illegal(s) for s in segments)
    assert all(s not in (".", "..") for s in segments)
    assert year is not None or " ()" not in segments[1]


@given(st.lists(st.text("ab/.", min_size=1, max_size=4), max_size=20))
def test_colliding_inputs_get_distinct_paths(names: list[str]) -> None:
    taken: set[str] = set()
    for n in names:
        rel = unique(f"x/{sanitize(n)}.flac", taken.__contains__)
        assert rel not in taken
        taken.add(rel)
    assert len(taken) == len(names)


def test_unique_suffix_is_deterministic() -> None:
    taken = {"a/b.flac", "a/b (2).flac"}
    assert unique("a/b.flac", taken.__contains__) == "a/b (3).flac"
    assert unique("c.flac", taken.__contains__) == "c.flac"
