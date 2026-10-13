"""Messaging and audit tables: messages, audit_log."""

import uuid

from sqlalchemy import ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase
from app.models.enums import MessageDirection, pg_enum


class Message(TimestampedBase):
    __tablename__ = "messages"

    worker_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workers.id", ondelete="CASCADE"), index=True
    )
    direction: Mapped[MessageDirection] = mapped_column(
        pg_enum(MessageDirection, "message_direction")
    )
    type: Mapped[str] = mapped_column(String(32))
    # Idempotency key: duplicate WhatsApp deliveries are ignored (PRD: Reliability).
    # Nullable because outbound rows get their ID only after the send succeeds.
    wa_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(32))


class AuditLog(TimestampedBase):
    """Consent, deletion and hiring events. No FK so entries outlive deleted rows."""

    __tablename__ = "audit_log"

    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
