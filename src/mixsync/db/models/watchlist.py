from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mixsync.db.base import Base, TimestampMixin


class WatchedEntity(TimestampMixin, Base):
    """One followed artist or label, checked once however many users follow it."""

    __tablename__ = "watched_entities"
    __table_args__ = (UniqueConstraint("kind", "mbid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))  # artist / label
    mbid: Mapped[str] = mapped_column(String(36))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_release_date: Mapped[str | None] = mapped_column(String(10))  # YYYY[-MM[-DD]]


class Follow(TimestampMixin, Base):
    __tablename__ = "follows"
    __table_args__ = (UniqueConstraint("user_id", "entity_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    entity_id: Mapped[int] = mapped_column(ForeignKey("watched_entities.id"))
    name: Mapped[str] = mapped_column(String(255))
    release_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    min_date: Mapped[str | None] = mapped_column(String(10))
    weekly_cap: Mapped[int | None] = mapped_column(Integer)  # None = unlimited


class FollowHit(TimestampMixin, Base):
    """What one follow did with one release group it found."""

    __tablename__ = "follow_hits"
    __table_args__ = (UniqueConstraint("follow_id", "release_group_mbid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    follow_id: Mapped[int] = mapped_column(ForeignKey("follows.id"))
    release_group_mbid: Mapped[str] = mapped_column(String(36))
    release_mbid: Mapped[str] = mapped_column(String(36))  # the release a request would use
    artist: Mapped[str] = mapped_column(String(255), default="")
    title: Mapped[str] = mapped_column(String(255), default="")
    release_date: Mapped[str] = mapped_column(String(10), default="")
    # requested / skipped_owned / skipped_requested / skipped_filter / skipped_cap
    status: Mapped[str] = mapped_column(String(20))
    prev_status: Mapped[str | None] = mapped_column(String(20))  # before a pick took the row
    request_id: Mapped[int | None] = mapped_column(ForeignKey("requests.id"))
