"""Tables linking workers and jobs: matches, applications, courses."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase
from app.models.enums import ApplicationStatus, pg_enum


class Match(TimestampedBase):
    __tablename__ = "matches"
    __table_args__ = (UniqueConstraint("worker_id", "job_id"),)

    worker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workers.id", ondelete="CASCADE"))
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    score: Mapped[float] = mapped_column(Float)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_response: Mapped[str | None] = mapped_column(String(32))


class Application(TimestampedBase):
    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("worker_id", "job_id"),)

    worker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workers.id", ondelete="CASCADE"))
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    status: Mapped[ApplicationStatus] = mapped_column(
        pg_enum(ApplicationStatus, "application_status"),
        default=ApplicationStatus.INTERESTED,
        server_default=ApplicationStatus.INTERESTED.value,
    )


class Course(TimestampedBase):
    """Curated IBM SkillsBuild links."""

    __tablename__ = "courses"

    skill: Mapped[str] = mapped_column(String(100), index=True)
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(8))
