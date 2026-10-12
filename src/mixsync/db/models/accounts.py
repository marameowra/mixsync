from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mixsync.db.base import Base, TimestampMixin


class UserLink(TimestampMixin, Base):
    """A user's account on an outside service. `secret_encrypted` is a Fernet token
    (db/secretbox.py). The plaintext must never be rendered, logged, or put in an error."""

    __tablename__ = "user_links"
    __table_args__ = (UniqueConstraint("user_id", "service"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    service: Mapped[str] = mapped_column(String(20))  # listenbrainz/navidrome
    external_username: Mapped[str] = mapped_column(String(255))
    secret_encrypted: Mapped[str] = mapped_column(Text)  # Fernet token
    needs_relink: Mapped[bool] = mapped_column(Boolean, default=False)  # key can't decrypt it
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
