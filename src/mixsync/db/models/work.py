from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from mixsync.core.jobs import JobKind, JobState
from mixsync.db.base import Base, TimestampMixin


def _enum(cls: type[StrEnum]) -> Enum:
    def values(_: object) -> list[str]:
        return [m.value for m in cls]

    return Enum(cls, native_enum=False, length=20, values_callable=values)


Status = Literal[
    "searching",
    "no_results",
    "partial",
    "downloading",
    "verifying",
    "importing",
    "review",
    "imported",
    "owned",
    "failed",
    "skipped",
    "unverified",
]


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_run_after", "status", "run_after"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[JobKind] = mapped_column(_enum(JobKind))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[JobState] = mapped_column(_enum(JobState), default=JobState.QUEUED)
    priority: Mapped[int] = mapped_column(default=0)
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None]
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    parent_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))


class Request(TimestampMixin, Base):
    """One requested release and where the acquire pipeline has got to."""

    __tablename__ = "requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    release_mbid: Mapped[str] = mapped_column(String(36))
    artist: Mapped[str] = mapped_column(String(255), default="")  # display; filled by the search
    album: Mapped[str] = mapped_column(String(255), default="")
    # searching/downloading/verifying/importing/review/imported/partial/owned/failed/no_results
    status: Mapped[Status] = mapped_column(String(16), default="searching")
    tried: Mapped[list[list[str]]] = mapped_column(JSON, default=list)  # [peer, folder] pairs
    no_results_attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_search_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None]  # human-readable; state lives in the columns below
    dead_end: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    partial_note: Mapped[str | None] = mapped_column(Text)  # "N of M tracks present"
    migration_id: Mapped[int | None] = mapped_column(
        ForeignKey("migrations.id")
    )  # old-library import
    source: Mapped[str] = mapped_column(
        String(16), default="manual"
    )  # manual / watchlist / drop / adopt
    release_group_mbid: Mapped[str | None] = mapped_column(String(36))  # when the creator knew it
    # discovery: import only the wanted recordings, into /data/discover
    provisional: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    recording_mbids: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")

    @property
    def from_files(self) -> bool:
        """Identified from files already on disk: nothing to search for."""
        return self.migration_id is not None or self.source in ("drop", "adopt")


class Migration(TimestampMixin, Base):
    """One re-import of the old library (docs/design/migration.md)."""

    __tablename__ = "migrations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))  # the admin who started it
    # preflight/ready/failed/running/cancelled
    status: Mapped[str] = mapped_column(String(16), default="preflight")
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # preflight results
    manifest_path: Mapped[str] = mapped_column(String(500), default="")
    verify: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # last byte-identical check
