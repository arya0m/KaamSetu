"""Process one inbound WhatsApp message: load state, run the state machine, persist.

FR-1: nothing is written to Postgres until the worker agrees. Before that the
step and chosen language live in Redis under the phone's salted hash, with a TTL.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AuditLog, Message, MessageDirection, Worker
from app.schemas.whatsapp import InboundMessage
from app.services import conversation
from app.services.conversation import Action, Event, State
from app.services.phone import PhoneCodec, get_phone_codec, hash_phone

logger = logging.getLogger(__name__)

PRECONSENT_PREFIX = "onboard:"


@dataclass(frozen=True)
class Outbound:
    to_phone: str
    body: str
    worker_id: uuid.UUID | None  # None before consent: outbound is not logged


def preconsent_key(phone_hash: str) -> str:
    return PRECONSENT_PREFIX + phone_hash


def _load_preconsent(client: redis.Redis, key: str) -> tuple[State, str | None]:
    raw = cast(str | None, client.get(key))
    if not raw:
        return State.NEW, None
    data = json.loads(raw)
    return State(data.get("state", State.NEW)), data.get("language")


def _record_inbound(session: Session, worker: Worker, msg: InboundMessage) -> bool:
    """Log the inbound message; False if this message ID was already processed."""
    stmt = (
        pg_insert(Message)
        .values(
            worker_id=worker.id,
            direction=MessageDirection.INBOUND,
            type=msg.kind,
            wa_message_id=msg.message_id,
            status="received",
        )
        .on_conflict_do_nothing(index_elements=["wa_message_id"])
        .returning(Message.id)
    )
    return session.execute(stmt).first() is not None


def _grant_consent(
    session: Session, msg: InboundMessage, phone_hash: str, language: str, codec: PhoneCodec
) -> Worker | None:
    """Create the worker and log consent. None if a concurrent message already did."""
    worker = Worker(
        wa_phone_hash=phone_hash,
        wa_phone_encrypted=codec.encode(msg.from_phone),
        language=language,
        consent_at=datetime.now(UTC),
        state=State.ACTIVE.value,
    )
    session.add(worker)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return None
    session.add(
        AuditLog(
            actor=f"worker:{worker.id}", action="consent", entity="workers", entity_id=worker.id
        )
    )
    _record_inbound(session, worker, msg)
    return worker


def process_inbound(
    session: Session,
    client: redis.Redis,
    msg: InboundMessage,
    codec: PhoneCodec | None = None,
) -> list[Outbound]:
    settings = get_settings()
    codec = codec or get_phone_codec()
    phone_hash = hash_phone(msg.from_phone)
    key = preconsent_key(phone_hash)

    worker = session.scalar(select(Worker).where(Worker.wa_phone_hash == phone_hash))
    if worker is not None:
        if not _record_inbound(session, worker, msg):
            logger.info("duplicate inbound message ignored")
            session.rollback()
            return []
        state, language = State(worker.state), worker.language
    else:
        state, language = _load_preconsent(client, key)

    event = Event(text=msg.text, has_media=msg.media_count > 0)
    outcome = conversation.handle(state, language, event, settings.privacy_notice_url)

    if outcome.action is Action.SAVE_PRECONSENT:
        payload = json.dumps({"state": outcome.state.value, "language": outcome.language})
        client.set(key, payload, ex=settings.preconsent_ttl_s)
    elif outcome.action is Action.FORGET:
        client.delete(key)
    elif outcome.action is Action.GRANT_CONSENT:
        assert outcome.language is not None  # noqa: S101  # consent is only asked after language
        worker = _grant_consent(session, msg, phone_hash, outcome.language, codec)
        if worker is None:
            logger.info("consent already recorded by a concurrent message")
            return []
        client.delete(key)

    session.commit()
    return [Outbound(msg.from_phone, outcome.reply, worker.id if worker else None)]
