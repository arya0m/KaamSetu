"""Employer-side tables: employers, jobs."""

import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import EMBEDDING_DIM, TimestampedBase
from app.models.enums import JobStatus, Trade, pg_enum


class Employer(TimestampedBase):
    __tablename__ = "employers"

    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))  # Argon2
    company_name: Mapped[str] = mapped_column(String(200))
    verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class Job(TimestampedBase):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("pay_min <= pay_max", name="pay_range"),
        CheckConstraint("openings >= 1", name="openings_positive"),
    )

    employer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employers.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    trade: Mapped[Trade] = mapped_column(pg_enum(Trade, "trade"))
    skills: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'")
    )
    pay_min: Mapped[int] = mapped_column(Integer)
    pay_max: Mapped[int] = mapped_column(Integer)
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    openings: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    status: Mapped[JobStatus] = mapped_column(
        pg_enum(JobStatus, "job_status"),
        default=JobStatus.DRAFT,
        server_default=JobStatus.DRAFT.value,
        index=True,
    )
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
