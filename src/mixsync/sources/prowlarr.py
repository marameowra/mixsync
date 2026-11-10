"""Torrent and Usenet candidates from Prowlarr's indexers (search only; see the README).

Fields of `GET /api/v1/search` (Prowlarr's OpenAPI `ReleaseResource`) this module reads:
`title` release name, `size` bytes, `indexer` indexer name, `protocol` "torrent" / "usenet",
`downloadUrl` Prowlarr's proxy link to the .torrent / .nzb (null for magnet-only results; it can
also redirect to a magnet), `magnetUrl` magnet link, `seeders` torrent seeders (null for Usenet).
"""

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urlsplit

import httpx
from rapidfuzz import fuzz, utils

from mixsync.core.audio import EXTENSIONS
from mixsync.core.errors import AuthError, PermanentError, TransientError
from mixsync.core.matching import Candidate, CandidateFile, TransferInfo
from mixsync.ratelimit.client import PoliteClient

log = logging.getLogger(__name__)

AUDIO = frozenset(e[1:] for e in EXTENSIONS)
AUDIO_CATEGORY = 3000  # Newznab "Audio"
MAX_FETCHES = 3  # .torrent / .nzb downloads per search
# An album is 20 MB (a short lossy EP) to 3 GB (long hi-res); outside that it ranks last.
MIN_BYTES, MAX_BYTES = 20_000_000, 3_000_000_000
_DISC_DIR = re.compile(r"^(?:cd|dis[ck])[\s._-]*\d+$", re.IGNORECASE)
_SUBJECT_NAME = re.compile(r'"([^"]+)"')


def bdecode(data: bytes) -> Any:
    """Decode bencode (BEP 3); raises ValueError on malformed input."""

    def parse(i: int) -> tuple[Any, int]:
        c = data[i : i + 1]
        if c == b"i":
            end = data.index(b"e", i)
            return int(data[i + 1 : end]), end + 1
        if c in (b"l", b"d"):
            items: list[Any] = []
            i += 1
            while data[i : i + 1] != b"e":
                if i >= len(data):
                    raise ValueError("unterminated bencode list")
                value, i = parse(i)
                items.append(value)
            return (items if c == b"l" else dict(zip(items[::2], items[1::2], strict=True))), i + 1
        if c.isdigit():
            colon = data.index(b":", i)
            start, n = colon + 1, int(data[i:colon])
            if start + n > len(data):
                raise ValueError("bencode string runs past the end")
            return data[start : start + n], start + n
        raise ValueError(f"bad bencode at byte {i}")

    return parse(0)[0]


def torrent_files(data: bytes) -> list[tuple[list[str], int]]:
    """(path parts under the torrent's name, size) of each file in a .torrent."""
    info = bdecode(data)[b"info"]
    name = info[b"name"].decode(errors="replace")
    if b"files" not in info:
        return [([name], info[b"length"])]
    return [
        ([name, *(p.decode(errors="replace") for p in f[b"path"])], f[b"length"])
        for f in info[b"files"]
    ]


def nzb_files(data: bytes, title: str) -> list[tuple[list[str], int]]:
    """(path parts, size) of each file in an .nzb; the name is the quoted part of the subject,
    the size the sum of its (yEnc-encoded, so ~2 % large) segments."""
    out: list[tuple[list[str], int]] = []
    for f in ET.fromstring(data).iter():  # expat >= 2.4 refuses entity-expansion bombs
        if f.tag.rpartition("}")[2] != "file":
            continue
        m = _SUBJECT_NAME.search(f.get("subject", ""))
        size = sum(int(s.get("bytes", 0)) for s in f.iter() if s.tag.endswith("segment"))
        if m:
            out.append(([title, m[1]], size))
    return out


def to_candidate(release: dict[str, Any], files: list[tuple[list[str], int]]) -> Candidate | None:
    """The release's audio files as one Candidate, or None when they span more than one album
    folder (a discography or pack; picking files from one is #328)."""
    audio: list[CandidateFile] = []
    folders: set[tuple[str, ...]] = set()
    cue = False
    for parts, size in files:
        ext = parts[-1].rpartition(".")[2].lower() if "." in parts[-1] else ""
        cue = cue or ext == "cue"
        if ext not in AUDIO:
            continue
        folder = parts[:-1]
        if len(folder) > 1 and _DISC_DIR.match(folder[-1]):
            folder = folder[:-1]  # CD1 / Disc 2 belong to the album folder above them
        folders.add(tuple(folder))
        audio.append(CandidateFile(path="\\".join(parts), size=size, extension=ext))
    if len(folders) > 1:
        log.info("prowlarr: %r spans %d album folders; skipped", release["title"], len(folders))
        return None
    return Candidate(
        username=release.get("indexer") or str(release.get("indexerId", "")),
        folder=release["title"],
        files=tuple(audio),
        has_cue=cue,
        seeders=release.get("seeders") or 0,
        source="prowlarr",
    )


class ProwlarrSource:
    """Search only: enqueue/status/clear raise until a download client (#326, #327) exists, and
    the worker does not register this source before then."""

    max_queries = 2  # the pipeline tries only the first 2 query variants here

    def __init__(self, client: PoliteClient, base_url: str, api_key: str) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._headers = {"X-Api-Key": api_key}

    async def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.get(url, headers=self._headers, **kwargs)
        except httpx.TransportError as e:
            raise TransientError(f"prowlarr unreachable: {e!r}") from e
        if response.status_code in (401, 403):
            raise AuthError("prowlarr rejected the API key; check MIXSYNC_PROWLARR_API_KEY")
        if response.status_code >= 500:
            raise TransientError(f"prowlarr returned {response.status_code}")
        return response

    async def search(self, query: str) -> list[Candidate]:
        """Releases ranked by title, then a plausible album size, then seeders; the top
        MAX_FETCHES with a .torrent / .nzb link get their file list fetched and are returned."""
        response = await self._get(
            f"{self._base}/api/v1/search",
            params={"query": query, "type": "search", "categories": AUDIO_CATEGORY},
        )
        if response.status_code >= 400:
            raise PermanentError(f"prowlarr search returned {response.status_code}")
        results: list[dict[str, Any]] = response.json()
        usable = [r for r in results if r.get("downloadUrl") and r.get("title")]  # v1: no magnets
        usable.sort(
            key=lambda r: (
                -fuzz.token_set_ratio(query, r["title"], processor=utils.default_process),
                not MIN_BYTES <= (r.get("size") or 0) <= MAX_BYTES,
                -(r.get("seeders") or 0),
            )
        )
        out: list[Candidate] = []
        for release in usable[:MAX_FETCHES]:
            files = await self._files(release)
            if files and (cand := to_candidate(release, files)):
                out.append(cand)
        return out

    async def _files(self, release: dict[str, Any]) -> list[tuple[list[str], int]]:
        url = release["downloadUrl"]
        if urlsplit(url)[:2] != urlsplit(self._base)[:2]:  # the API key goes to Prowlarr only
            log.warning("prowlarr: %r links off Prowlarr (%s); skipped", release["title"], url)
            return []
        response = await self._get(url)
        if response.status_code != 200:  # a 3xx here is a redirect to a magnet link
            log.info("prowlarr: %r download gave %d", release["title"], response.status_code)
            return []
        try:
            if release.get("protocol") == "usenet":
                return nzb_files(response.content, release["title"])
            return torrent_files(response.content)
        except (ValueError, KeyError, TypeError, AttributeError, ET.ParseError) as e:
            log.info("prowlarr: %r has an unreadable file list: %r", release["title"], e)
            return []

    async def enqueue(self, candidate: Candidate) -> None:
        raise PermanentError("no torrent / Usenet download client is configured")

    async def status(self, candidate: Candidate) -> TransferInfo:
        raise PermanentError("no torrent / Usenet download client is configured")

    async def active_downloads(self) -> dict[str, int]:
        return {}

    async def clear(self, candidate: Candidate) -> None:
        return None
