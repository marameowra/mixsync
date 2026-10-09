from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from mixsync.core.jobs import JobKind, JobState
from mixsync.db.base import Base, TimestampMixin


def _enum(cls: type[StrEnum]) -> Enum:
    def values(_: object) -> list[str]:
        return [m.value for m in cls]

    return Enum(cls, native_enum=False, length=20, values_callable=values)


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"

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
    # searching/downloading/verifying/importing/review/imported/failed/no_results
    status: Mapped[str] = mapped_column(String(16), default="searching")
    tried: Mapped[list[list[str]]] = mapped_column(JSON, default=list)  # [peer, folder] pairs
    no_results_attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_search_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None]
