from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from mixsync.db.base import Base, TimestampMixin


class MatchDecision(TimestampMixin, Base):
    """One scored match with its feature vector and final action: the calibration data."""

    __tablename__ = "match_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))  # the requester
    stage: Mapped[int] = mapped_column(Integer)  # 1 pre-download, 2 post-download
    release_mbid: Mapped[str] = mapped_column(String(36))
    band: Mapped[str] = mapped_column(String(16))
    distance: Mapped[float] = mapped_column(Float)
    breakdown: Mapped[list[dict[str, Any]]] = mapped_column(JSON)  # key, penalty, weight, reason
    vetoes: Mapped[list[str]] = mapped_column(JSON)
    scorer_version: Mapped[int] = mapped_column(Integer)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16))
    final_action: Mapped[str | None] = mapped_column(String(16))
    chosen_release_mbid: Mapped[str | None] = mapped_column(String(36))  # repick
    acted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
