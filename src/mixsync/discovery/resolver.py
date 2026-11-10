import re

from mixsync.core.matching import Band, FileTrack, TrackInfo
from mixsync.core.types import RecordingRef
from mixsync.discovery.listenbrainz import ListenBrainzClient
from mixsync.match.profiles import BALANCED, band
from mixsync.match.scorer import track_distance
from mixsync.metadata.musicbrainz import MusicBrainzProvider

_ISRC = re.compile(r"[A-Z]{2}[A-Z0-9]{3}\d{7}")


def _confident(artist: str, title: str, ref: RecordingRef) -> float | None:
    """The match distance, or None when it is below the auto-accept gate."""
    if not ref.artist:  # track_distance skips the artist check against an empty credit
        return None
    dist = track_distance(
        FileTrack(title=title, artist=artist), TrackInfo(ref.title, ref.artist), incl_artist=True
    ).distance
    return dist if band(dist, [], BALANCED) is Band.AUTO_ACCEPT else None


class TrackResolver:
    """Upstream track to a MusicBrainz recording; None is an unresolved track, never a guess."""

    def __init__(self, mb: MusicBrainzProvider, lb: ListenBrainzClient) -> None:
        self._mb = mb
        self._lb = lb

    async def resolve(
        self, artist: str, title: str, isrc: str | None = None, lb_token: str | None = None
    ) -> RecordingRef | None:
        code = (isrc or "").replace("-", "").strip().upper()
        if _ISRC.fullmatch(code):
            # The ISRC names the recording, so only the artist is checked (against a wrongly
            # assigned ISRC); upstream titles carry suffixes like "- Remastered 2008".
            scored = [
                (d, r)
                for r in await self._mb.isrc_recordings(code)
                if (d := _confident(artist, r.title, r)) is not None
            ]
            if scored:
                return min(scored, key=lambda t: t[0])[1]
        if not lb_token:  # the lookup needs a user token; without one the track stays unresolved
            return None
        found = await self._lb.lookup(artist, title, lb_token)
        return found if found and _confident(artist, title, found) is not None else None
