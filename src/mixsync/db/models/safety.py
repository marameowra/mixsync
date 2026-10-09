from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from mixsync.db.base import Base, TimestampMixin


class FileOp(TimestampMixin, Base):
    __tablename__ = "file_ops"
    __table_args__ = (
        Index("ix_file_ops_status", "status"),
        Index("ix_file_ops_batch_id", "batch_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    src: Mapped[str] = mapped_column(Text)
    dst: Mapped[str] = mapped_column(Text)
    src_hash: Mapped[str | None] = mapped_column(String(128))
    dst_hash: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class TagSnapshot(TimestampMixin, Base):
    __tablename__ = "tag_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    track_id: Mapped[int | None] = mapped_column(Integer)  # no FK until tracks arrives in Phase 2
    file_path: Mapped[str] = mapped_column(Text)
    op_id: Mapped[int] = mapped_column(ForeignKey("file_ops.id"))
    tags: Mapped[dict[str, Any]] = mapped_column(JSON)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
