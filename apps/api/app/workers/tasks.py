"""Background tasks. The webhook only queues; all conversation work happens here."""

import logging
import uuid
from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import update
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.db import get_sessionmaker
from app.models import Message, MessageDirection
from app.redis import get_redis
from app.schemas.whatsapp import InboundMessage, StatusUpdate
from app.services.inbound import process_inbound
from app.services.whatsapp import SendThrottle, TransientSendError, get_provider
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# PRD: retries with exponential backoff, max 3 attempts (1 try + 2 retries).
RETRY_POLICY: dict[str, Any] = {
    "retry_backoff": True,
    "retry_backoff_max": 30,
    "retry_jitter": True,
    "max_retries": 2,
}


@celery_app.task(name="kaamsetu.ping")  # type: ignore[untyped-decorator]
def ping() -> str:
    return "pong"


@celery_app.task(  # type: ignore[untyped-decorator]
    name="kaamsetu.handle_inbound",
    autoretry_for=(OperationalError, RedisConnectionError),
    **RETRY_POLICY,
)
def handle_inbound(message: dict[str, Any]) -> None:
    msg = InboundMessage.model_validate(message)
    with get_sessionmaker()() as session:
        outbound = process_inbound(session, get_redis(), msg)
    for out in outbound:
        send_text.delay(out.to_phone, out.body, str(out.worker_id) if out.worker_id else None)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="kaamsetu.send_text",
    autoretry_for=(TransientSendError,),
    **RETRY_POLICY,
)
def send_text(to_phone: str, body: str, worker_id: str | None = None) -> None:
    settings = get_settings()
    SendThrottle(get_redis(), settings.whatsapp_min_send_interval_ms).acquire()
    callback = f"{settings.public_base_url.rstrip('/')}/webhooks/whatsapp/status"
    provider_id = get_provider().send_text(to_phone, body, callback)
    if worker_id is None:
        return  # before consent nothing is stored, including outbound messages
    with get_sessionmaker()() as session:
        session.add(
            Message(
                worker_id=uuid.UUID(worker_id),
                direction=MessageDirection.OUTBOUND,
                type="text",
                wa_message_id=provider_id,
                status="sent",
            )
        )
        session.commit()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="kaamsetu.update_message_status",
    autoretry_for=(OperationalError,),
    **RETRY_POLICY,
)
def update_message_status(status: dict[str, Any]) -> None:
    update_ = StatusUpdate.model_validate(status)
    with get_sessionmaker()() as session:
        session.execute(
            update(Message)
            .where(Message.wa_message_id == update_.message_id)
            .values(status=update_.status)
        )
        session.commit()
