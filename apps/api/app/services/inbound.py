"""Process one inbound WhatsApp message: load state, run the state machine, persist.

FR-1: nothing is written to Postgres until the worker agrees. Before that the
step and chosen language live in Redis under the phone's salted hash, with a TTL.

FR-2 to FR-5: after consent, a voice note or text is stored as a transcript
(voice_notes), the combined transcript is extracted into a profile, and the
worker gets either one follow-up question or the summary to confirm.
"""

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

import redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import i18n
from app.ai.llm import LLMClient, get_llm
from app.ai.retry import with_retries
from app.ai.stt import AudioTooLong, SpeechToText, STTError, get_stt, transcribe_audio
from app.config import get_settings
from app.models import AuditLog, Message, MessageDirection, VoiceNote, Worker, WorkerProfile
from app.schemas.profile import WorkerProfileData
from app.schemas.whatsapp import InboundMessage
from app.services import conversation
from app.services.conversation import Action, Ctx, Event, State
from app.services.phone import PhoneCodec, get_phone_codec, hash_phone
from app.services.profile import extract_profile
from app.services.whatsapp import MediaError, TransientMediaError, WhatsAppProvider, get_provider

logger = logging.getLogger(__name__)

PRECONSENT_PREFIX = "onboard:"
MAX_TRANSCRIPTS = 10  # newest inputs used for extraction


@dataclass(frozen=True)
class Outbound:
    to_phone: str
    body: str
    worker_id: uuid.UUID | None  # None before consent: outbound is not logged


@dataclass
class Deps:
    """External services, injectable for tests."""

    stt: SpeechToText = field(default_factory=lambda: get_stt())
    llm: LLMClient = field(default_factory=lambda: get_llm())
    provider: WhatsAppProvider = field(default_factory=lambda: get_provider())
    codec: PhoneCodec = field(default_factory=lambda: get_phone_codec())
    # Sends a message right away (e.g. "listening…" before slow work).
    notify: Callable[[Outbound], None] = lambda _: None


def preconsent_key(phone_hash: str) -> str:
    return PRECONSENT_PREFIX + phone_hash


def _load_preconsent(client: redis.Redis, key: str) -> tuple[Ctx, str | None]:
    raw = cast(str | None, client.get(key))
    if not raw:
        return Ctx(State.NEW), None
    data = json.loads(raw)
    return Ctx(State(data.get("state", State.NEW))), data.get("language")


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
        state=State.AWAITING_DESCRIPTION.value,
    )
    session.add(worker)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return None
    audit = AuditLog(
        actor=f"worker:{worker.id}", action="consent", entity="workers", entity_id=worker.id
    )
    session.add(audit)
    _record_inbound(session, worker, msg)
    return worker


def _profile_row(session: Session, worker: Worker) -> WorkerProfile | None:
    return session.scalar(select(WorkerProfile).where(WorkerProfile.worker_id == worker.id))


def _stored_profile(row: WorkerProfile | None) -> WorkerProfileData | None:
    if row is None:
        return None
    try:
        return WorkerProfileData.model_validate(row.profile_json)
    except ValueError:
        logger.warning("stored profile no longer validates; ignoring it")
        return None


def _transcripts(session: Session, worker: Worker) -> list[str]:
    rows = session.scalars(
        select(VoiceNote.transcript)
        .where(VoiceNote.worker_id == worker.id, VoiceNote.transcript.is_not(None))
        .order_by(VoiceNote.created_at.desc(), VoiceNote.id.desc())
        .limit(MAX_TRANSCRIPTS)
    ).all()
    return [t for t in reversed(rows) if t]


def _capture_input(
    session: Session, worker: Worker, msg: InboundMessage, language: str, deps: Deps
) -> str | None:
    """Store this input as a transcript (FR-2). Returns an error reply key, or None."""
    if not msg.is_audio:
        assert msg.text is not None  # noqa: S101  # the state machine only passes text or audio
        session.add(
            VoiceNote(worker_id=worker.id, media_url=None, transcript=msg.text, language=language)
        )
        return None

    settings = get_settings()
    deps.notify(Outbound(msg.from_phone, i18n.t("voice_listening", language), worker.id))
    try:
        audio = with_retries(
            lambda: deps.provider.fetch_media(cast(str, msg.media_url), settings.media_max_bytes),
            retry_on=(TransientMediaError,),
            label="media",
        )
        transcript = transcribe_audio(deps.stt, audio, language)
    except AudioTooLong:
        return "voice_too_long"
    except (MediaError, STTError):
        logger.warning("voice note could not be transcribed", exc_info=True)
        return "voice_failed"
    if not transcript.text.strip():
        return "voice_failed"
    session.add(
        VoiceNote(
            worker_id=worker.id,
            media_url=msg.media_url,
            transcript=transcript.text,
            language=transcript.language,
            duration_s=round(transcript.duration_s, 2),
        )
    )
    return None


def _process_input(
    session: Session, worker: Worker, ctx: Ctx, language: str, msg: InboundMessage, deps: Deps
) -> conversation.Outcome:
    error = _capture_input(session, worker, msg, language, deps)
    if error is not None:
        return conversation.Outcome(ctx, language, Action.NONE, i18n.t(error, language))
    session.flush()

    row = _profile_row(session, worker)
    focus = ctx.field if ctx.state is State.FOLLOW_UP else None
    profile = extract_profile(deps.llm, _transcripts(session, worker), _stored_profile(row), focus)
    outcome = conversation.after_extraction(ctx, language, profile, get_settings().max_follow_ups)
    if outcome.action is Action.SAVE_PROFILE and profile is not None:
        data = profile.model_dump(mode="json")
        if row is None:
            session.add(WorkerProfile(worker_id=worker.id, profile_json=data, confirmed=False))
        else:
            row.profile_json, row.confirmed = data, False
    return outcome


def _post_consent(
    session: Session,
    worker: Worker,
    msg: InboundMessage,
    deps: Deps,
    voice_languages: frozenset[str],
) -> list[Outbound]:
    try:
        ctx = Ctx.decode(worker.state)
    except ValueError:
        logger.warning("unknown worker state; restarting profile flow")
        ctx = Ctx(State.AWAITING_DESCRIPTION)
    language = worker.language or "en"
    event = Event(text=msg.text, has_media=msg.media_count > 0, is_audio=msg.is_audio)
    outcome = conversation.handle(
        ctx, language, event, get_settings().privacy_notice_url, voice_languages
    )

    if outcome.action is Action.PROCESS_INPUT:
        outcome = _process_input(session, worker, ctx, language, msg, deps)
    elif outcome.action is Action.CONFIRM_PROFILE:
        row = _profile_row(session, worker)
        if row is None:  # nothing to confirm: start the profile again
            outcome = conversation.Outcome(
                Ctx(State.AWAITING_DESCRIPTION), language, Action.NONE,
                i18n.describe_work(language, language in voice_languages),
            )  # fmt: skip
        else:
            row.confirmed = True
    elif outcome.action is Action.SHOW_SUMMARY:
        profile = _stored_profile(_profile_row(session, worker))
        if profile is None:
            outcome = conversation.Outcome(
                Ctx(State.AWAITING_DESCRIPTION), language, Action.NONE,
                i18n.describe_work(language, language in voice_languages),
            )  # fmt: skip
        else:
            outcome = conversation.Outcome(
                outcome.ctx, language, Action.NONE, i18n.profile_summary(profile, language)
            )

    worker.state = outcome.ctx.encode()
    session.commit()
    assert outcome.reply is not None  # noqa: S101  # every resolved outcome has a reply
    return [Outbound(msg.from_phone, outcome.reply, worker.id)]


def process_inbound(
    session: Session, client: redis.Redis, msg: InboundMessage, deps: Deps | None = None
) -> list[Outbound]:
    settings = get_settings()
    deps = deps or Deps()
    voice_languages = frozenset(c for c in i18n.LANGUAGE_CODES if deps.stt.supports(c))
    phone_hash = hash_phone(msg.from_phone)
    key = preconsent_key(phone_hash)

    worker = session.scalar(select(Worker).where(Worker.wa_phone_hash == phone_hash))
    if worker is not None:
        if not _record_inbound(session, worker, msg):
            logger.info("duplicate inbound message ignored")
            session.rollback()
            return []
        return _post_consent(session, worker, msg, deps, voice_languages)

    ctx, language = _load_preconsent(client, key)
    event = Event(text=msg.text, has_media=msg.media_count > 0, is_audio=msg.is_audio)
    outcome = conversation.handle(
        ctx, language, event, settings.privacy_notice_url, voice_languages
    )

    if outcome.action is Action.SAVE_PRECONSENT:
        payload = json.dumps({"state": outcome.state.value, "language": outcome.language})
        client.set(key, payload, ex=settings.preconsent_ttl_s)
    elif outcome.action is Action.FORGET:
        client.delete(key)
    elif outcome.action is Action.GRANT_CONSENT:
        assert outcome.language is not None  # noqa: S101  # consent is only asked after language
        worker = _grant_consent(session, msg, phone_hash, outcome.language, deps.codec)
        if worker is None:
            logger.info("consent already recorded by a concurrent message")
            return []
        client.delete(key)

    session.commit()
    assert outcome.reply is not None  # noqa: S101  # pre-consent outcomes always reply
    return [Outbound(msg.from_phone, outcome.reply, worker.id if worker else None)]
