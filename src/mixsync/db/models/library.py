from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from mixsync.db.base import Base, TimestampMixin


class CanonicalRelease(TimestampMixin, Base):
    """The one release of a release group that tracks are filed under."""

    __tablename__ = "canonical_releases"

    release_group_mbid: Mapped[str] = mapped_column(String(36), primary_key=True)
    release_mbid: Mapped[str] = mapped_column(String(36))


class Track(TimestampMixin, Base):
    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(Text, unique=True)  # relative to the library root
    recording_mbid: Mapped[str | None] = mapped_column(String(36))
    release_mbid: Mapped[str | None] = mapped_column(String(36))
    release_group_mbid: Mapped[str | None] = mapped_column(String(36))
    artist_mbids: Mapped[list[str]] = mapped_column(JSON, default=list)
    acoustid_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(16))  # verified | unverified
    batch_id: Mapped[str] = mapped_column(String(64))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Plan(TimestampMixin, Base):
    """A dry-run plan for a bulk change, kept so it can be run and then undone."""

    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # merge
    release_group_mbid: Mapped[str] = mapped_column(String(36))
    canonical_release_mbid: Mapped[str] = mapped_column(String(36))
    previous_canonical_mbid: Mapped[str | None] = mapped_column(String(36))  # None: none stored
    canonical_set: Mapped[bool] = mapped_column(default=False)  # this plan stored/switched it
    batch_id: Mapped[str] = mapped_column(String(64), default="")
    # draft | running | done | failed | undoing | undone
    status: Mapped[str] = mapped_column(String(16), default="draft")
    ops: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text)
