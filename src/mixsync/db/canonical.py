from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mixsync.db.models.library import CanonicalRelease


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
