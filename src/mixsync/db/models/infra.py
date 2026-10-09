from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from mixsync.db.base import Base


class RateBucket(Base):
    __tablename__ = "rate_buckets"

    service: Mapped[str] = mapped_column(String(32), primary_key=True)
    tokens: Mapped[float] = mapped_column(Float)
    capacity: Mapped[float] = mapped_column(Float)
    refill_per_sec: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HttpCache(Base):
    __tablename__ = "http_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    service: Mapped[str] = mapped_column(String(32))
    status: Mapped[int] = mapped_column(Integer)
    headers: Mapped[dict[str, Any]] = mapped_column(JSON)
    body: Mapped[bytes] = mapped_column(LargeBinary)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
