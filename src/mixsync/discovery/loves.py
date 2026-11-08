"""The ListenBrainz loves upstream: one "Loves" mirrored playlist per user, newest love first.
ListenBrainz imports Last.fm loves, so this covers Last.fm with no Last.fm call."""

from datetime import datetime

from sqlalchemy.orm import Session

from mixsync.core.errors import PermanentError
from mixsync.db.models.discovery import MirroredPlaylist
from mixsync.discovery.listenbrainz import ListenBrainzClient
from mixsync.discovery.mirrored import LOVES, UpstreamTrack, create
from mixsync.discovery.playlists import Discovery

NAME, REF = "Loves", "loves"  # the user's ListenBrainz name is read at sync time from user_links


def create_loves(s: Session, now: datetime, user_id: int) -> MirroredPlaylist:
    """The user's Loves mirror, made on the first call (a mirror already named "Loves" raises
    IntegrityError)."""
    return create(s, now, user_id, NAME, LOVES, REF, cadence_days=1)


class Loves:
    def __init__(self, discovery: Discovery, client: ListenBrainzClient) -> None:
        self.discovery, self.client = discovery, client

    async def tracks(self, user_id: int, ref: str) -> list[UpstreamTrack]:
        with self.discovery.sessions() as s:
            links, why = self.discovery.links(s, user_id, frozenset({"listenbrainz"}))
        if links is None:
            raise PermanentError(why)
        user = links["listenbrainz"][0]
        return [
            UpstreamTrack(r.artist, r.title, recording_mbid=r.recording_mbid)
            for r in await self.client.loves(user)
        ]
