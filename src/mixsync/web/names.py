"""Names for MusicBrainz ids shown to users (`names` in the page context, see macros/mb.html).

Order: the mb_name table, local rows, then the response cache, then (detail pages only) a few
cached, rate-limited lookups. List pages pass no lookups, so they never call MusicBrainz."""

import logging
import re
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from mixsync.acquire.evidence import Evidence
from mixsync.core.errors import PermanentError, TransientError
from mixsync.core.protocols import NameLookup
from mixsync.db.models.matching import MatchDecision
from mixsync.db.models.work import Request
from mixsync.db.names import put_names, stored_names

log = logging.getLogger(__name__)

Ref = tuple[str, str]  # (musicbrainz kind, mbid)
MAX_LOOKUPS = 3  # a detail page waits for at most this many 1 req/s lookups
_UUID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)


def _local(db: Session, releases: set[str]) -> dict[str, str]:
    """Decisions (evidence target) and requests already hold a release's title and artist."""
    found: dict[str, str] = {}
    if not releases:
        return found
    for mbid, ev in db.execute(
        select(MatchDecision.release_mbid, MatchDecision.evidence).where(
            MatchDecision.release_mbid.in_(releases)
        )
    ):
        t = Evidence.parse(ev).target
        if t.get("title"):
            found[mbid] = f"{t['title']} — {t.get('artist', '')}"
    for mbid, artist, album in db.execute(
        select(Request.release_mbid, Request.artist, Request.album).where(
            Request.release_mbid.in_(releases), Request.album != ""
        )
    ):
        found.setdefault(mbid, f"{album} — {artist}")
    return found


async def resolve(
    db: Session, namer: NameLookup, refs: set[Ref], lookups: int = 0
) -> dict[str, str]:
    """mbid -> name for what can be named; a miss is left out. `lookups` caps the MusicBrainz
    calls (0 on list pages)."""
    refs = {r for r in refs if _UUID.fullmatch(r[1])}
    found = stored_names(db, {m for _, m in refs})
    found = _local(db, {m for k, m in refs if k == "release"} - found.keys()) | found
    learned: list[tuple[str, str, str]] = []
    committed = bool(lookups)
    if lookups:  # the request transaction must not hold the SQLite lock while the bucket needs it
        db.commit()
    for kind, mbid in sorted(
        (r for r in refs if r[1] not in found), key=lambda r: (r[0] == "recording", r)
    ):
        try:
            name = await namer.label(kind, mbid, cached_only=True)
            if name is None and lookups > 0:
                lookups -= 1
                name = await namer.label(kind, mbid)
        except (TransientError, PermanentError, httpx.HTTPError, ValueError) as e:
            log.info("no name for %s %s: %s", kind, mbid, e)
            continue
        if name:
            found[mbid] = name
            learned.append((kind, mbid, name))
    if learned and not committed:
        put_names(db, datetime.now(UTC), learned)
    elif learned:  # the request transaction is closed: save in a short one of its own
        with Session(db.get_bind()) as s, s.begin():
            put_names(s, datetime.now(UTC), learned)
    return found
