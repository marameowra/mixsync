from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mixsync.db.base import Base, TimestampMixin


class UserLink(TimestampMixin, Base):
    """A user's account on an outside service. `secret` is plaintext (the DB lives under
    /config) and must never be rendered, logged, or put in an error message."""

    __tablename__ = "user_links"
    __table_args__ = (UniqueConstraint("user_id", "service"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    service: Mapped[str] = mapped_column(String(20))  # listenbrainz/navidrome
    username: Mapped[str] = mapped_column(String(255))
    secret: Mapped[str] = mapped_column(String(255))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
