from mixsync.core.matching import Band, FileTrack, TrackInfo
from mixsync.core.types import RecordingRef
from mixsync.discovery.listenbrainz import ListenBrainzClient
from mixsync.match.profiles import BALANCED, band
from mixsync.match.scorer import track_distance
from mixsync.metadata.musicbrainz import MusicBrainzProvider


def _confident(artist: str, title: str, ref: RecordingRef) -> float | None:
    """The match distance, or None when it is below the auto-accept gate."""
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
        self, artist: str, title: str, isrc: str | None = None
    ) -> RecordingRef | None:
        if isrc:
            scored = [
                (d, r)
                for r in await self._mb.isrc_recordings(isrc)
                if (d := _confident(artist, title, r)) is not None
            ]
            if scored:
                return min(scored, key=lambda t: t[0])[1]
        found = await self._lb.lookup(artist, title)
        return found if found and _confident(artist, title, found) is not None else None
