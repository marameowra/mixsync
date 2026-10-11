"""Harmony release lookup link.

`/release?url=<store url>` checked 2026-10-10 against https://github.com/kellnerd/harmony
(server/state.ts reads `searchParams.getAll('url')`; the project README shows
https://harmony.pulsewidth.org.uk/release?url=...).
"""

from urllib.parse import urlencode, urlsplit

HARMONY_RELEASE_URL = "https://harmony.pulsewidth.org.uk/release"


def harmony_url(store_url: str) -> str:
    store_url = store_url.strip()
    parts = urlsplit(store_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"not an http(s) URL: {store_url!r}")
    return f"{HARMONY_RELEASE_URL}?{urlencode({'url': store_url})}"
