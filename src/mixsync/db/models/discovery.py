from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from mixsync.db.base import Base, TimestampMixin


class ProvisionalTrack(TimestampMixin, Base):
    """A discovery track imported under /data/discover (never a `tracks` row)."""

    __tablename__ = "provisional_tracks"
    # a trashed row keeps its path, so the same path can be imported (active) again
    __table_args__ = (
        Index(
            "uq_provisional_tracks_path",
            "path",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(Text)  # relative to the discover root
    recording_mbid: Mapped[str] = mapped_column(String(36), index=True)
    release_mbid: Mapped[str | None] = mapped_column(String(36))
    release_group_mbid: Mapped[str | None] = mapped_column(String(36))
    acoustid_id: Mapped[str | None] = mapped_column(String(36))
    batch_id: Mapped[str] = mapped_column(String(64))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/promoted/trashed
    navidrome_song_id: Mapped[str | None] = mapped_column(String(64))
    promoted_path: Mapped[str | None] = mapped_column(Text)
    trash_path: Mapped[str | None] = mapped_column(Text)


class DiscoveryPlaylist(TimestampMixin, Base):
    """A user's generated playlist: what it draws on, how big, and how long unowned tracks stay."""

    __tablename__ = "discovery_playlists"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    # [{"type": "weekly_exploration" | "recommendations" | "lb_radio", "prompt": ..., "mode": ...}]
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    size: Mapped[int] = mapped_column(Integer, default=50)
    unowned_ratio: Mapped[float] = mapped_column(Float, default=0.3)
    cadence_days: Mapped[int] = mapped_column(Integer, default=7)
    retention_days: Mapped[int] = mapped_column(Integer, default=30)  # slice 7 reads these two
    keep_plays: Mapped[int] = mapped_column(Integer, default=3)
    navidrome_playlist_id: Mapped[str | None] = mapped_column(String(64))
    next_build_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # the last build's picks: [{"recording_mbid", "artist", "title"}], in playlist order
    entries: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    last_error: Mapped[str | None] = mapped_column(Text)


class ProvisionalHold(TimestampMixin, Base):
    """One user's claim on a recording that is not in the main library. Retention (slice 7)
    decides keep / promote / expire from these rows."""

    __tablename__ = "provisional_holds"
    __table_args__ = (UniqueConstraint("user_id", "recording_mbid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    recording_mbid: Mapped[str] = mapped_column(String(36), index=True)
    playlist_id: Mapped[int] = mapped_column(ForeignKey("discovery_playlists.id"))
    request_id: Mapped[int | None] = mapped_column(ForeignKey("requests.id"))
    provisional_track_id: Mapped[int | None] = mapped_column(ForeignKey("provisional_tracks.id"))
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )  # null until imported
    kept_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    keep_reason: Mapped[str | None] = mapped_column(String(16))
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
