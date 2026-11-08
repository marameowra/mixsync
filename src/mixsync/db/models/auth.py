from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
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


class AppSettings(TimestampMixin, Base):
    """Admin-edited, app-wide settings: one row, id 1. No row means every default."""

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    min_auto_accept_confidence: Mapped[int | None] = mapped_column(Integer)  # percent; None: no cap
    # App settings (ADR 0021): NULL is not saved; mixsync.db.app_settings resolves them at startup
    mb_contact: Mapped[str | None] = mapped_column(Text)
    mb_rate: Mapped[float | None] = mapped_column(Float)
    mb_base_url: Mapped[str | None] = mapped_column(Text)
    deezer_suggestions: Mapped[bool | None]
    discogs_suggestions: Mapped[bool | None]
    slskd_url: Mapped[str | None] = mapped_column(Text)
    slskd_max_searches: Mapped[int | None]
    slskd_search_debounce: Mapped[int | None]
    max_concurrent_downloads: Mapped[int | None]
    slow_download_window: Mapped[int | None]
    slow_download_floor: Mapped[int | None]
    slow_download_share: Mapped[float | None] = mapped_column(Float)
    navidrome_url: Mapped[str | None] = mapped_column(Text)
    navidrome_user: Mapped[str | None] = mapped_column(Text)
    navidrome_library: Mapped[str | None] = mapped_column(Text)
    navidrome_discover_library: Mapped[str | None] = mapped_column(Text)
    adopt_downloads: Mapped[bool | None]
    adopt_settle_minutes: Mapped[int | None]
    flac_level: Mapped[int | None]
    canonical_media: Mapped[list[str] | None] = mapped_column(JSON)
    canonical_countries: Mapped[list[str] | None] = mapped_column(JSON)
    trash_days: Mapped[int | None]
    timezone: Mapped[str | None] = mapped_column(Text)
    path_template: Mapped[str | None] = mapped_column(Text)
    log_level: Mapped[str | None] = mapped_column(Text)


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
