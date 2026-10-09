from mixsync.core.matching import AlbumInfo, TrackInfo
from mixsync.core.types import ReleaseRef
from mixsync.metadata._http import get_json
from mixsync.metadata.models import MbCredit, MbRelease, MbSearch
from mixsync.ratelimit.client import PoliteClient

VARIOUS_ARTISTS = "89ad4ac3-39f7-470e-963a-56509c546377"
_INC = "recordings artist-credits media labels release-groups"  # httpx sends spaces as +


def _credit(credits: list[MbCredit]) -> str:
    return "".join(c.name + c.joinphrase for c in credits)


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and date[:4].isdigit() else None


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
    )


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class MusicBrainzProvider:
    def __init__(self, client: PoliteClient, base_url: str = "https://musicbrainz.org") -> None:
        self._client = client
        self._base = base_url.rstrip("/") + "/ws/2"

    async def get_release(self, mbid: str) -> AlbumInfo:
        data = await get_json(
            self._client, f"{self._base}/release/{mbid}", {"inc": _INC, "fmt": "json"}
        )
        return _album(MbRelease.model_validate(data))

    async def search_releases(self, artist: str, album: str, limit: int = 10) -> list[ReleaseRef]:
        data = await get_json(
            self._client,
            f"{self._base}/release",
            {
                "query": f"release:{_quote(album)} AND artist:{_quote(artist)}",
                "limit": limit,
                "fmt": "json",
            },
        )
        return [
            ReleaseRef(r.id, r.title, _credit(r.artist_credit), r.date, r.country, r.track_count)
            for r in MbSearch.model_validate(data).releases
        ]
