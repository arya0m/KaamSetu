"""Worker-side tables: workers, worker_profiles, voice_notes."""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import EMBEDDING_DIM, TimestampedBase


class Worker(TimestampedBase):
    __tablename__ = "workers"

    # Salted hash for lookup; encrypted copy for sending (PRD: Security).
    wa_phone_hash: Mapped[str] = mapped_column(String(64), unique=True)
    wa_phone_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    language: Mapped[str | None] = mapped_column(String(8))
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Drives the conversation state machine (states are defined in Phase 2).
    state: Mapped[str] = mapped_column(String(32))


class WorkerProfile(TimestampedBase):
    __tablename__ = "worker_profiles"

    # One active profile per worker.
    worker_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workers.id", ondelete="CASCADE"), unique=True
    )
    profile_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)


class VoiceNote(TimestampedBase):
    __tablename__ = "voice_notes"

    worker_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workers.id", ondelete="CASCADE"), index=True
    )
    media_url: Mapped[str] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(8))
    duration_s: Mapped[float | None] = mapped_column(Float)
