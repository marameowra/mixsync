from html.parser import HTMLParser

import pytest

from mixsync.web.templating import templates


class Elements(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def _art(title: str, src: str | None = None) -> str:
    return templates.env.from_string(
        '{% from "macros/media.html" import artwork %}{{ artwork(title, src, "lg") }}'
    ).render(title=title, src=src)


def test_artwork_missing_cover_is_a_labelled_title_initial() -> None:
    text = _art("Blue Train")
    dom = Elements()
    dom.feed(text)
    assert "missing" in (dom.tags[0][1].get("class") or "").split()
    assert dom.tags[0][1]["aria-label"] == "No cover art for Blue Train"
    assert ">B</span>" in text
    assert not any(tag in ("img", "object") for tag, _ in dom.tags)


def test_artwork_cover_is_a_lazy_img_over_the_letter() -> None:
    text = _art("Blue Train", "/media/cover/42")
    dom = Elements()
    dom.feed(text)
    img = next(attrs for tag, attrs in dom.tags if tag == "img")
    assert "missing" not in (dom.tags[0][1].get("class") or "").split()
    assert img.get("src") == "/media/cover/42" and img.get("loading") == "lazy"
    assert img.get("alt") == ""  # a failed load renders nothing, so the letter below shows
    assert dom.tags[0][1]["aria-label"] == "Cover art for Blue Train"
    assert text.index(">B</span>") < text.index("<img ")
    assert "onerror" not in text and "<object" not in text


def test_artwork_id_adds_hx_preserve_only_with_a_cover() -> None:
    def render(src: str | None) -> str:
        return templates.env.from_string(
            '{% from "macros/media.html" import artwork %}{{ artwork("T", src, "xs", "art-1") }}'
        ).render(src=src)

    assert 'id="art-1" hx-preserve' in render("/media/cover/42")
    assert "hx-preserve" not in render(None)


@pytest.mark.parametrize(
    "src",
    [
        "/media/artwork/release/22222222-2222-2222-2222-222222222222",
        "/media/artwork/release/22222222-2222-2222-2222-222222222222?group=33333333-3333-3333-3333-333333333333",
        "/media/artwork/release-group/33333333-3333-3333-3333-333333333333",
    ],
)
def test_artwork_accepts_typed_same_origin_musicbrainz_routes(src: str) -> None:
    text = _art("Blue Train", src)
    dom = Elements()
    dom.feed(text)
    img = next(attrs for tag, attrs in dom.tags if tag == "img")
    assert img.get("src") == src


@pytest.mark.parametrize(
    "src",
    [
        "/media/artwork/release/not-a-uuid",
        "/media/artwork/release/gggggggg-gggg-gggg-gggg-gggggggggggg",
        "/media/artwork/release/22222222-2222-2222-2222-222222222222?group=bad",
        "/media/artwork/release-group/33333333-3333-3333-3333-333333333333?group=22222222-2222-2222-2222-222222222222",
        "/media/artwork/release/22222222-2222-2222-2222-222222222222/extra",
    ],
)
def test_artwork_rejects_malformed_musicbrainz_routes(src: str) -> None:
    text = _art("Blue Train", src)
    assert "<img" not in text


@pytest.mark.parametrize(
    "src",
    [
        "https://coverartarchive.org/release/x/front",
        "//example.org/cover",
        "/media/cover/../42",
        "/media/cover/42?url=x",
    ],
)
def test_artwork_never_embeds_untrusted_or_external_sources(src: str) -> None:
    text = _art('<img src=x onerror="bad()">', src)
    assert "<object" not in text and "<img " not in text
    assert "&lt;img" in text
    assert "No cover art for" in text
