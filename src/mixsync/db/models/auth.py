from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from mixsync.db.base import Base, TimestampMixin


class MatchingProfile(TimestampMixin, Base):
    __tablename__ = "matching_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    preset: Mapped[str] = mapped_column(String(20))  # strict/balanced/loose/custom
    auto_accept_max: Mapped[float] = mapped_column(Float)
    review_max: Mapped[float] = mapped_column(Float)
    quality_pref: Mapped[Any] = mapped_column(JSON, nullable=True)


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active/disabled
    matching_profile_id: Mapped[int | None] = mapped_column(ForeignKey("matching_profiles.id"))


class Role(TimestampMixin, Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)


class RoleCapability(TimestampMixin, Base):
    __tablename__ = "role_capabilities"

    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), primary_key=True)
    capability: Mapped[str] = mapped_column(String(50), primary_key=True)


class UserRole(TimestampMixin, Base):
    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), primary_key=True)


class Session(TimestampMixin, Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 hex of the token
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(255))
