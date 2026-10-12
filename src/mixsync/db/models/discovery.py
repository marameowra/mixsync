from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, text
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
