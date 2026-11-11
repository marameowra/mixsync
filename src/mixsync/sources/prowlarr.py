"""Torrent and Usenet candidates from Prowlarr's indexers; torrents download through qBittorrent,
NZBs through SABnzbd (see the README).

Fields of `GET /api/v1/search` (Prowlarr's OpenAPI `ReleaseResource`) this module reads:
`title` release name, `size` bytes, `indexer` indexer name, `protocol` "torrent" / "usenet",
`downloadUrl` Prowlarr's proxy link to the .torrent / .nzb (omitted when unset; it may redirect
once to the indexer, or to a magnet), `seeders` torrent seeders. Null fields are omitted.
"""

import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from importlib.metadata import version
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from rapidfuzz import fuzz, utils

from mixsync.core.audio import EXTENSIONS
from mixsync.core.errors import AuthError, PermanentError, TransientError
from mixsync.core.matching import Candidate, CandidateFile, TransferInfo
from mixsync.ratelimit.client import PoliteClient
from mixsync.sources.qbittorrent import Qbittorrent
from mixsync.sources.sabnzbd import Sabnzbd

log = logging.getLogger(__name__)

AUDIO = frozenset(e[1:] for e in EXTENSIONS)
AUDIO_CATEGORY = 3000  # Newznab "Audio"
MAX_FETCHES = 3  # .torrent / .nzb downloads per search
# An album is 20 MB (a short lossy EP) to 3 GB (long hi-res); outside that it ranks last.
MIN_BYTES, MAX_BYTES = 20_000_000, 3_000_000_000
_DISC_DIR = re.compile(r"^(?:cd|dis[ck])[\s._-]*\d+$", re.IGNORECASE)
_SUBJECT_NAME = re.compile(r'"([^"]+)"')


MAX_BENCODE_DEPTH = 64


def _parse(data: bytes, i: int, depth: int = 0) -> tuple[Any, int]:
    """The bencode value at byte i, and the index just past it."""
    if depth > MAX_BENCODE_DEPTH:  # a hostile .torrent must not hit RecursionError
        raise ValueError("bencode nested too deep")
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
            value, i = _parse(data, i, depth + 1)
            items.append(value)
        return (items if c == b"l" else dict(zip(items[::2], items[1::2], strict=True))), i + 1
    if c.isdigit():
        colon = data.index(b":", i)
        start, n = colon + 1, int(data[i:colon])
        if start + n > len(data):
            raise ValueError("bencode string runs past the end")
        return data[start : start + n], start + n
    raise ValueError(f"bad bencode at byte {i}")


def bdecode(data: bytes) -> Any:
    """Decode bencode (BEP 3); raises ValueError on malformed input."""
    return _parse(data, 0)[0]


def info_hash(data: bytes) -> str:
    """A .torrent's v1 info hash: the SHA-1 of its `info` dict's bytes as they are in the file.
    shortcut: a v2-only torrent has none that qBittorrent reports the same way; its download
    then fails and the next candidate is tried. Map v2 hashes if such torrents turn up."""
    if data[:1] != b"d":
        raise ValueError("a .torrent is not a bencode dict")
    i = 1
    while data[i : i + 1] != b"e":
        if i >= len(data):
            raise ValueError("unterminated bencode dict")
        key, start = _parse(data, i, 1)
        _, i = _parse(data, start, 1)
        if key == b"info":
            return hashlib.sha1(data[start:i], usedforsecurity=False).hexdigest()
    raise ValueError("a .torrent has no info dict")


def nzb_hash(files: list[tuple[list[str], int]]) -> str:
    """An NZB's identity: the SHA-1 of its file list, which survives an indexer re-serving the
    same release with other headers or comments."""
    return hashlib.sha1(repr(files).encode(), usedforsecurity=False).hexdigest()


def without_key(url: str) -> str:
    """The link minus its `apikey` query param: candidates are stored, the key never is."""
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    query = urlencode([(k, v) for k, v in pairs if k.lower() != "apikey"])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def torrent_files(data: bytes) -> list[tuple[list[str], int]]:
    """(path parts under the torrent's name, size) of each file in a .torrent."""
    info = bdecode(data)[b"info"]
    name = info[b"name"].decode(errors="replace")
    if b"files" not in info:
        files = [([name], info[b"length"])]
    else:
        files = [
            ([name, *(p.decode(errors="replace") for p in f[b"path"])], f[b"length"])
            for f in info[b"files"]
        ]
    if not all(isinstance(size, int) for _, size in files):
        raise ValueError("a .torrent file length is not an integer")
    return files


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


def to_candidate(
    release: dict[str, Any], files: list[tuple[list[str], int]], infohash: str = "", nzb: str = ""
) -> Candidate | None:
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
        url=without_key(release.get("downloadUrl") or ""),
        infohash=infohash,
        nzb_hash=nzb,
    )


class ProwlarrSource:
    """Search, with torrents downloaded through qBittorrent and NZBs through SABnzbd. A release
    with no client to download it raises at enqueue/status; the worker passes `protocols` for the
    configured clients only, so search never offers one."""

    max_queries = 2  # the pipeline tries only the first 2 query variants here

    def __init__(
        self,
        client: PoliteClient,
        base_url: str,
        api_key: str,
        torrents: Qbittorrent | None = None,
        protocols: frozenset[str] = frozenset({"torrent", "usenet"}),
        usenet: Sabnzbd | None = None,
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._headers = {"X-Api-Key": api_key}
        self._torrents = torrents
        self._usenet = usenet
        self._protocols = protocols  # others are skipped before any fetch: nothing downloads them

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

    async def ping(self) -> None:
        """One authenticated status call (the settings page's Test button); never a search."""
        response = await self._get(f"{self._base}/api/v1/system/status")
        if response.status_code != 200:
            raise PermanentError(f"prowlarr system/status returned {response.status_code}")

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
        usable = [  # v1: no magnets
            r
            for r in results
            if r.get("downloadUrl")
            and r.get("title")
            and r.get("protocol", "torrent") in self._protocols
        ]
        usable.sort(
            key=lambda r: (
                -fuzz.token_set_ratio(query, r["title"], processor=utils.default_process),
                not MIN_BYTES <= (r.get("size") or 0) <= MAX_BYTES,
                -(r.get("seeders") or 0),
            )
        )
        out: list[Candidate] = []
        for release in usable[:MAX_FETCHES]:
            files, infohash = await self._files(release)
            nzb = nzb_hash(files) if release.get("protocol") == "usenet" else ""
            if files and (cand := to_candidate(release, files, infohash, nzb)):
                out.append(cand)
        return out

    async def _download(self, url: str) -> httpx.Response:
        """The .torrent / .nzb behind a Prowlarr link. Prowlarr answers a Usenet link (and a
        torrent one when the indexer's Redirect is on) with a 301 to the indexer's own URL: one
        http(s) hop is followed, without Prowlarr's key header. That URL carries the indexer's
        key, so it is never stored or logged. A magnet, or a second redirect, comes back as is."""
        response = await self._get(url)
        if not response.is_redirect:
            return response
        target = response.url.join(response.headers.get("location", ""))
        if target.scheme not in ("http", "https"):
            return response
        try:
            # a plain User-Agent: MIXSYNC_MB_CONTACT is not for third-party indexers
            agent = {"User-Agent": f"MixSync/{version('mixsync')}"}
            response = await self._client.get(str(target), headers=agent)
        except httpx.TransportError as e:  # never the URL: it has the indexer's key
            raise TransientError(f"indexer unreachable ({type(e).__name__})") from e
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientError(f"the indexer returned {response.status_code}")
        return response

    def _ours(self, url: str) -> bool:
        return urlsplit(url)[:2] == urlsplit(self._base)[:2]  # the API key goes to Prowlarr only

    async def _files(self, release: dict[str, Any]) -> tuple[list[tuple[list[str], int]], str]:
        """The release's file list, and a torrent's info hash."""
        url: str = release["downloadUrl"]
        if not self._ours(url):
            host = urlsplit(url).netloc  # never the full URL: its query carries the API key
            log.warning("prowlarr: %r links off Prowlarr (%s); skipped", release["title"], host)
            return [], ""
        try:
            response = await self._download(url)
        except TransientError as e:  # one bad file fetch skips that release, not the search
            log.info("prowlarr: %r download failed: %s", release["title"], e)
            return [], ""
        if response.status_code != 200:  # a 3xx here is a magnet or a second redirect
            log.info("prowlarr: %r download gave %d", release["title"], response.status_code)
            return [], ""
        try:
            if release.get("protocol") == "usenet":
                return nzb_files(response.content, release["title"]), ""
            return torrent_files(response.content), info_hash(response.content)
        except (ValueError, KeyError, TypeError, AttributeError, ET.ParseError) as e:
            log.info("prowlarr: %r has an unreadable file list: %r", release["title"], e)
            return [], ""

    def _client_for(self, candidate: Candidate) -> Qbittorrent | Sabnzbd:
        """The client for the candidate's protocol: an info hash is a torrent, an NZB hash an
        NZB."""
        if self._torrents is not None and candidate.infohash:
            return self._torrents
        if self._usenet is not None and candidate.nzb_hash:
            return self._usenet
        raise PermanentError("no download client is configured for this release")

    async def enqueue(self, candidate: Candidate) -> None:
        """Fetch the .torrent / .nzb again (the API key is added back here, never stored) and
        hand its bytes to qBittorrent or SABnzbd, so no key lands in either client."""
        client = self._client_for(candidate)
        if not self._ours(candidate.url):
            raise PermanentError("the release's link is not on Prowlarr")
        sep = "&" if urlsplit(candidate.url).query else "?"  # httpx `params=` would drop `link`
        response = await self._download(f"{candidate.url}{sep}{urlencode({'apikey': self._key})}")
        if response.status_code != 200:
            raise PermanentError(f"prowlarr download returned {response.status_code}")
        try:
            if isinstance(client, Sabnzbd):
                got = nzb_hash(nzb_files(response.content, candidate.folder))
            else:
                got = info_hash(response.content)
        except (ValueError, ET.ParseError) as e:
            raise PermanentError(f"unreadable download file: {e}") from e
        if got != (candidate.nzb_hash or candidate.infohash):  # never download what was not scored
            raise PermanentError("the release file changed since the search")
        if isinstance(client, Sabnzbd):
            await client.add(response.content, candidate.nzb_hash)
        else:
            await client.add(response.content, candidate.infohash)

    async def status(self, candidate: Candidate) -> TransferInfo:
        return await self._client_for(candidate).status(candidate)

    async def active_downloads(self) -> dict[str, int]:
        return {}  # the clients queue their own jobs; the Soulseek cap is not theirs

    async def clear(self, candidate: Candidate) -> None:
        """Remove an unfinished torrent with its partial files (a finished one seeds on), or
        MixSync's SABnzbd job (with its files only if it never completed)."""
        if self._torrents is not None and candidate.infohash:
            await self._torrents.remove_unfinished(candidate.infohash)
        elif self._usenet is not None and candidate.nzb_hash:
            await self._usenet.remove(candidate.nzb_hash)
