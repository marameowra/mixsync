import logging
from typing import Any

from mixsync.core.matching import AlbumInfo, TrackInfo
from mixsync.core.types import EntityRef, ReleaseGroupHit, ReleaseRef
from mixsync.metadata._http import get_json
from mixsync.metadata.models import (
    MbCredit,
    MbEntity,
    MbEntitySearch,
    MbGenre,
    MbRelease,
    MbReleaseGroup,
    MbRgSearch,
    MbSearch,
)
from mixsync.ratelimit.client import PoliteClient

log = logging.getLogger(__name__)

DISCOGRAPHY_PAGES = 5  # the picker reads at most this many pages of 100
WATCH_LIMIT = 100  # rows per page of a watch search
# shortcut: a watch search reads at most 10 pages (1000 rows); raise it if a label outgrows that.
WATCH_MAX_PAGES = 10
VARIOUS_ARTISTS = "89ad4ac3-39f7-470e-963a-56509c546377"
_INC = "recordings artist-credits media labels release-groups genres"  # httpx sends spaces as +


def _credit(credits: list[MbCredit]) -> str:
    return "".join(c.name + c.joinphrase for c in credits)


def _artist_ids(credits: list[MbCredit]) -> tuple[str, ...]:
    return tuple(c.artist.id for c in credits)


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and date[:4].isdigit() else None


def _genres(r: MbRelease) -> tuple[str, ...]:
    """Release group's genres, else the release's, else the primary artist's; most votes first."""
    levels: list[list[MbGenre]] = [
        r.release_group.genres if r.release_group else [],
        r.genres,
        r.artist_credit[0].artist.genres if r.artist_credit else [],
    ]
    found: list[MbGenre] = next(filter(None, levels), None) or []
    return tuple(g.name for g in sorted(found, key=lambda g: (-g.count, g.name)))


def _album(r: MbRelease) -> AlbumInfo:
    tracks: list[TrackInfo] = []
    for medium in sorted(r.media, key=lambda m: m.position):
        for t in medium.tracks:
            ms = t.length if t.length is not None else t.recording.length
            tracks.append(
                TrackInfo(
                    title=t.title,
                    artist=_credit(t.artist_credit or t.recording.artist_credit) or None,
                    length=None if ms is None else ms / 1000,
                    index=len(tracks) + 1,
                    medium_index=t.position,
                    medium=medium.position,
                    recording_id=t.recording.id,
                    artist_ids=_artist_ids(t.artist_credit or t.recording.artist_credit),
                )
            )
    label = next((li for li in r.label_info if li.label), None)
    return AlbumInfo(
        artist=_credit(r.artist_credit),
        album=r.title,
        tracks=tuple(tracks),
        va=any(c.artist.id == VARIOUS_ARTISTS for c in r.artist_credit),
        year=_year(r.date),
        original_year=_year(r.release_group.first_release_date) if r.release_group else None,
        country=r.country,
        label=label.label.name if label and label.label else None,
        catalognum=next((li.catalog_number for li in r.label_info if li.catalog_number), None),
        albumdisambig=r.disambiguation or None,
        media=next((m.format for m in r.media if m.format), None),
        mediums=len(r.media),
        release_id=r.id,
        release_group_id=r.release_group.id if r.release_group else None,
        artist_ids=_artist_ids(r.artist_credit),
        genres=_genres(r),
    )


def _rg_type(primary: str | None, secondary: list[str]) -> str:
    low = {s.lower() for s in secondary}
    return next((t for t in ("compilation", "live") if t in low), (primary or "other").lower())


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _hit(
    g: MbReleaseGroup, title: str, date: str | None, release_ids: list[str], credit: list[MbCredit]
) -> ReleaseGroupHit:
    return ReleaseGroupHit(
        g.id or "",
        title,
        _credit(credit),
        _rg_type(g.primary_type, g.secondary_types),
        date or "",
        tuple(release_ids),
    )


def _fresh(fresh: bool) -> dict[str, Any]:
    """Request kwargs that skip the response cache (a re-check or poll wants today's answer)."""
    return {"headers": {"Cache-Control": "no-cache"}} if fresh else {}


class MusicBrainzProvider:
    def __init__(self, client: PoliteClient, base_url: str = "https://musicbrainz.org") -> None:
        self._client = client
        self._base = base_url.rstrip("/") + "/ws/2"

    async def get_release(self, mbid: str, *, fresh: bool = False) -> AlbumInfo:
        data = await get_json(
            self._client,
            f"{self._base}/release/{mbid}",
            {"inc": _INC, "fmt": "json"},
            **_fresh(fresh),
        )
        return _album(MbRelease.model_validate(data))

    async def search_releases(
        self, artist: str, album: str, limit: int = 10, *, fresh: bool = False
    ) -> list[ReleaseRef]:
        terms = [f"{k}:{_quote(v)}" for k, v in (("release", album), ("artist", artist)) if v]
        if not terms:  # an empty query is no question
            return []
        data = await get_json(
            self._client,
            f"{self._base}/release",
            {
                "query": " AND ".join(terms),
                "limit": limit,
                "fmt": "json",
            },
            **_fresh(fresh),
        )
        return [
            ReleaseRef(r.id, r.title, _credit(r.artist_credit), r.date, r.country, r.track_count)
            for r in MbSearch.model_validate(data).releases
        ]

    async def browse_group_releases(self, release_group_mbid: str) -> list[ReleaseRef]:
        # shortcut: one page (100 releases); a larger group is ranked on its first 100 only.
        data = await get_json(
            self._client,
            f"{self._base}/release",
            {"release-group": release_group_mbid, "inc": "media", "fmt": "json", "limit": 100},
        )
        return [
            ReleaseRef(
                r.id,
                r.title,
                "",
                r.date or None,  # MB sends "" for an unknown date
                r.country,
                sum(m.track_count for m in r.media),
                tuple(m.format for m in r.media if m.format),
                r.status,
            )
            for r in MbSearch.model_validate(data).releases
        ]

    async def new_release_groups(
        self, kind: str, mbid: str, since: str | None, *, fresh: bool = False
    ) -> list[ReleaseGroupHit]:
        """A paged search, one request per page. `mbid` must be a UUID (the caller parses it). An artist is a
        release-group search on `firstreleasedate`; a label a release search on `date`, mapped
        to release groups. Pages are read while the search `count` says more remain."""
        artist = kind == "artist"
        field, date_field = ("arid", "firstreleasedate") if artist else ("laid", "date")
        query = f"{field}:{mbid}" + (f" AND {date_field}:[{since} TO *]" if since else "")
        by_group: dict[str, ReleaseGroupHit] = {}
        offset = 0
        for _ in range(WATCH_MAX_PAGES):
            data = await get_json(
                self._client,
                f"{self._base}/{'release-group' if artist else 'release'}",
                {"query": query, "limit": WATCH_LIMIT, "offset": offset, "fmt": "json"},
                **_fresh(fresh),
            )
            if artist:
                rows: list[Any] = MbRgSearch.model_validate(data).release_groups
                for g in rows:
                    if g.id:
                        by_group[g.id] = _hit(
                            g,
                            g.title,
                            g.first_release_date,
                            [r.id for r in g.releases],
                            g.artist_credit,
                        )
            else:
                rows = MbSearch.model_validate(data).releases
                for r in rows:
                    if (g := r.release_group) is None or not g.id:
                        continue
                    old = by_group.get(g.id)
                    ids = [*(old.release_ids if old else ()), r.id]
                    date = min((d for d in (old and old.date, r.date) if d), default="")
                    by_group[g.id] = _hit(g, g.title or r.title, date, ids, r.artist_credit)
            offset += len(rows)
            if not rows or offset >= data.get("count", 0):
                break
        else:
            log.warning(
                "watch search for %s %s hit the page cap; older hits are unread", kind, mbid
            )
        hits = list(by_group.values())
        return [h for h in hits if h.date]

    async def search_entities(self, kind: str, name: str) -> list[EntityRef]:
        if not name.strip():
            return []
        data = await get_json(
            self._client,
            f"{self._base}/{kind}",
            {"query": _quote(name), "limit": 10, "fmt": "json"},
        )
        found = MbEntitySearch.model_validate(data)
        return [EntityRef(e.id, e.name, e.disambiguation) for e in found.artists + found.labels]

    async def entity_name(self, kind: str, mbid: str) -> str:
        data = await get_json(self._client, f"{self._base}/{kind}/{mbid}", {"fmt": "json"})
        return MbEntity.model_validate(data).name

    async def artist_discography(self, mbid: str) -> tuple[list[ReleaseGroupHit], bool]:
        """Browse the artist's release groups, 100 a page, at most DISCOGRAPHY_PAGES pages.
        Returns them and whether the list was cut short. Only the back-catalogue picker calls it."""
        found: list[ReleaseGroupHit] = []
        for page in range(DISCOGRAPHY_PAGES):
            data = await get_json(
                self._client,
                f"{self._base}/release-group",
                {"artist": mbid, "limit": WATCH_LIMIT, "offset": page * WATCH_LIMIT, "fmt": "json"},
            )
            body = MbRgSearch.model_validate(data)
            found += [
                _hit(g, g.title, g.first_release_date, [], []) for g in body.release_groups if g.id
            ]
            if (page + 1) * WATCH_LIMIT >= body.count or not body.release_groups:
                return found, False
        return found, True
