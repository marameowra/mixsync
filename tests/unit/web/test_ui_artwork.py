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
    assert ("span", {"role": "img", "aria-label": "No cover art for Blue Train"}) in dom.tags
    assert ">B</span>" in text
    assert not any(tag in ("img", "object") for tag, _ in dom.tags)


def test_artwork_cover_has_native_http_failure_fallback() -> None:
    text = _art("Blue Train", "/media/cover/42")
    dom = Elements()
    dom.feed(text)
    obj = next(attrs for tag, attrs in dom.tags if tag == "object")
    assert "missing" not in (dom.tags[0][1].get("class") or "").split()
    assert obj.get("data") == "/media/cover/42"
    assert "type" not in obj  # The cover proxy can return either JPEG or PNG.
    assert 'aria-label="No cover art for Blue Train"' in text
    assert text.index("<object ") < text.index('role="img"') < text.index("</object>")
    assert "onerror" not in text


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
    assert "<object" not in text and "<img" not in text
    assert "&lt;img" in text
    assert "No cover art for" in text
