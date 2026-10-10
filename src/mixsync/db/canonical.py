from collections.abc import Iterable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mixsync.db.models.library import CanonicalRelease, Track


def get(s: Session, group_mbid: str) -> str | None:
    row = s.get(CanonicalRelease, group_mbid)
    return row.release_mbid if row else None


def store(s: Session, group_mbid: str, release_mbid: str) -> str:
    """Store the choice unless another worker got there first; returns the one that stands."""
    try:
        with s.begin_nested():
            s.add(CanonicalRelease(release_group_mbid=group_mbid, release_mbid=release_mbid))
    except IntegrityError:
        pass
    return get(s, group_mbid) or release_mbid


def switch(s: Session, group_mbid: str, old: str, new: str) -> str:
    """Make `new` canonical if the row still holds `old`; returns the value that stands."""
    s.execute(
        update(CanonicalRelease)
        .where(
            CanonicalRelease.release_group_mbid == group_mbid, CanonicalRelease.release_mbid == old
        )
        .values(release_mbid=new)
    )
    s.expire_all()
    return get(s, group_mbid) or new


def owned(
    s: Session, group_mbid: str, recordings: Iterable[str], batch_id: str = ""
) -> set[str | None]:
    """Which of the recordings the group already has in the library (not counting one batch)."""
    return set(
        s.scalars(
            select(Track.recording_mbid).where(
                Track.recording_mbid.is_not(None),
                Track.release_group_mbid == group_mbid,
                Track.recording_mbid.in_(list(recordings)),
                Track.batch_id != batch_id,
            )
        )
    )
