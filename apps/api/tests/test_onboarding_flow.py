"""FR-1 end to end: webhook → queue → state machine → DB → reply (mocked WhatsApp).

Runs against a real PostgreSQL (TEST_DATABASE_URL); Redis is fakeredis and
Celery runs eagerly, so the whole loop happens inside one test.
"""

from collections.abc import Iterator
from itertools import count

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from app import i18n
from app.config import get_settings
from app.db import get_engine, get_sessionmaker
from app.models import AuditLog, Message, MessageDirection, Worker
from app.services.inbound import preconsent_key
from app.services.phone import hash_phone
from app.workers import tasks
from app.workers.celery_app import celery_app
from tests.conftest import FakeWhatsApp, signed_post, twilio_inbound

pytestmark = pytest.mark.db

PATH = "/webhooks/whatsapp"
HINDI_PHONE = "+919876500001"
ENGLISH_PHONE = "+919876500002"
URL = "https://kaamsetu.test/privacy"
ALL_TABLES = (
    "workers", "worker_profiles", "voice_notes", "employers", "jobs",
    "matches", "applications", "courses", "messages", "audit_log",
)  # fmt: skip


@pytest.fixture
def loop(
    migrated: Engine,
    client: TestClient,
    fake_redis: fakeredis.FakeRedis,
    fake_whatsapp: FakeWhatsApp,
    eager_celery: None,
) -> Iterator["Loop"]:
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    yield Loop(client, fake_whatsapp, fake_redis, migrated)
    get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


class Loop:
    def __init__(
        self, client: TestClient, wa: FakeWhatsApp, r: fakeredis.FakeRedis, engine: Engine
    ) -> None:
        self.client, self.wa, self.redis, self.engine = client, wa, r, engine
        self._ids = count(1)

    def send(self, phone: str, body: str = "", num_media: int = 0, sid: str | None = None) -> str:
        sid = sid or f"SM{next(self._ids):032d}"
        resp = signed_post(self.client, PATH, twilio_inbound(sid, phone, body, num_media))
        assert resp.status_code == 200, resp.text
        return sid

    def last_reply(self, phone: str) -> str:
        return self.wa.bodies_to(phone)[-1]

    def rows(self, table: str) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608

    def total_rows(self) -> int:
        return sum(self.rows(t) for t in ALL_TABLES)

    def worker(self, phone: str) -> Worker | None:
        with Session(self.engine) as s:
            return s.scalar(select(Worker).where(Worker.wa_phone_hash == hash_phone(phone)))


@pytest.mark.parametrize(
    ("phone", "choice", "agree", "language"),
    [(HINDI_PHONE, "1", "हाँ", "hi"), (ENGLISH_PHONE, "English", "1", "en")],
)
def test_fr1_onboarding_completes(
    loop: Loop, phone: str, choice: str, agree: str, language: str
) -> None:
    loop.send(phone, "hello")
    assert loop.last_reply(phone) == i18n.welcome(get_settings().privacy_notice_url)

    loop.send(phone, choice)
    assert loop.last_reply(phone) == i18n.t("consent_request", language, privacy_url=URL)
    assert loop.total_rows() == 0, "FR-1: nothing stored before consent"

    loop.send(phone, agree)
    assert loop.last_reply(phone) == i18n.t("consent_thanks", language)

    worker = loop.worker(phone)
    assert worker is not None
    assert worker.language == language
    assert worker.state == "active"
    assert worker.consent_at is not None
    assert worker.wa_phone_hash != phone and phone not in worker.wa_phone_hash
    assert loop.redis.get(preconsent_key(hash_phone(phone))) is None

    with Session(loop.engine) as s:
        audit = s.scalars(select(AuditLog)).all()
        assert [(a.action, a.entity, a.entity_id) for a in audit] == [
            ("consent", "workers", worker.id)
        ]
        directions = s.scalars(select(Message.direction).order_by(Message.created_at)).all()
        assert directions == [MessageDirection.INBOUND, MessageDirection.OUTBOUND]

    loop.send(phone, "काम चाहिए")
    assert loop.last_reply(phone) == i18n.echo("काम चाहिए", language)


def test_fr1_decline_stores_nothing(loop: Loop) -> None:
    loop.send(HINDI_PHONE, "hi")
    loop.send(HINDI_PHONE, "1")
    loop.send(HINDI_PHONE, "2")
    assert loop.last_reply(HINDI_PHONE) == i18n.t("consent_declined", "hi")
    assert loop.total_rows() == 0
    assert loop.redis.keys("onboard:*") == []

    loop.send(HINDI_PHONE, "hi again")  # starts over
    assert loop.last_reply(HINDI_PHONE) == i18n.welcome(get_settings().privacy_notice_url)


def test_fr1_preconsent_state_expires(loop: Loop) -> None:
    loop.send(ENGLISH_PHONE, "hi")
    ttl: int = loop.redis.ttl(preconsent_key(hash_phone(ENGLISH_PHONE)))  # type: ignore[assignment]
    assert 0 < ttl <= get_settings().preconsent_ttl_s


def test_fr1_invalid_answers_reprompt(loop: Loop) -> None:
    loop.send(ENGLISH_PHONE, "hi")
    loop.send(ENGLISH_PHONE, "klingon")
    assert loop.last_reply(ENGLISH_PHONE) == i18n.language_retry()
    loop.send(ENGLISH_PHONE, "2")
    loop.send(ENGLISH_PHONE, "", num_media=1)
    assert loop.last_reply(ENGLISH_PHONE) == i18n.t("consent_invalid", "en")
    assert loop.total_rows() == 0


def test_p2_duplicate_delivery_processed_once(loop: Loop) -> None:
    for _ in range(3):
        loop.send(ENGLISH_PHONE, "hi", sid="SM_DUP")
    assert len(loop.wa.bodies_to(ENGLISH_PHONE)) == 1


def test_p2_duplicate_after_consent_is_ignored_by_db_unique(loop: Loop) -> None:
    for body in ("hi", "2", "1"):
        loop.send(ENGLISH_PHONE, body)
    loop.send(ENGLISH_PHONE, "echo me", sid="SM_AGAIN")
    loop.redis.delete("wa:seen:SM_AGAIN")  # simulate the Redis claim expiring or being lost
    loop.send(ENGLISH_PHONE, "echo me", sid="SM_AGAIN")
    assert loop.wa.bodies_to(ENGLISH_PHONE).count(i18n.echo("echo me", "en")) == 1


def test_p2_workers_are_isolated(loop: Loop) -> None:
    loop.send(HINDI_PHONE, "hi")
    loop.send(ENGLISH_PHONE, "hi")
    loop.send(HINDI_PHONE, "1")
    loop.send(ENGLISH_PHONE, "2")
    assert loop.last_reply(HINDI_PHONE) == i18n.t("consent_request", "hi", privacy_url=URL)
    assert loop.last_reply(ENGLISH_PHONE) == i18n.t("consent_request", "en", privacy_url=URL)


def test_p2_status_callback_updates_outbound_message(loop: Loop) -> None:
    for body in ("hi", "2", "1"):
        loop.send(ENGLISH_PHONE, body)
    with Session(loop.engine) as s:
        out = s.scalars(select(Message).where(Message.direction == MessageDirection.OUTBOUND)).one()
    assert out.wa_message_id is not None
    resp = signed_post(
        loop.client, PATH + "/status", {"MessageSid": out.wa_message_id, "MessageStatus": "read"}
    )
    assert resp.status_code == 200
    with Session(loop.engine) as s:
        assert s.get(Message, out.id).status == "read"  # type: ignore[union-attr]


def test_p2_concurrent_consent_creates_one_worker(loop: Loop) -> None:
    """Two "yes" messages processed at once must not create two workers."""
    for body in ("hi", "2"):
        loop.send(ENGLISH_PHONE, body)
    state = str(loop.redis.get(preconsent_key(hash_phone(ENGLISH_PHONE))))
    loop.send(ENGLISH_PHONE, "1")
    loop.redis.set(preconsent_key(hash_phone(ENGLISH_PHONE)), state)  # replay the old state
    loop.send(ENGLISH_PHONE, "yes")
    with Session(loop.engine) as s:
        assert s.scalar(select(func.count()).select_from(Worker)) == 1


def test_p2_send_task_retries_transient_failures(
    loop: Loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.whatsapp import TransientSendError

    attempts = {"n": 0}
    real_send = loop.wa.send_text

    def flaky(to: str, body: str, cb: str | None) -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TransientSendError("503")
        return real_send(to, body, cb)

    monkeypatch.setattr(loop.wa, "send_text", flaky)
    monkeypatch.setattr(tasks.send_text, "retry_backoff", False)
    # Let Celery's eager runner re-apply retries itself instead of raising Retry.
    monkeypatch.setattr(celery_app.conf, "task_eager_propagates", False)
    loop.send(ENGLISH_PHONE, "hi")
    assert attempts["n"] == 3  # succeeded on the 3rd attempt
    assert len(loop.wa.bodies_to(ENGLISH_PHONE)) == 1


def test_p2_send_gives_up_after_three_attempts(loop: Loop, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.whatsapp import TransientSendError

    attempts = {"n": 0}

    def always_down(to: str, body: str, cb: str | None) -> str:
        attempts["n"] += 1
        raise TransientSendError("503")

    monkeypatch.setattr(loop.wa, "send_text", always_down)
    monkeypatch.setattr(tasks.send_text, "retry_backoff", False)
    monkeypatch.setattr(celery_app.conf, "task_eager_propagates", False)
    loop.send(ENGLISH_PHONE, "hi")
    assert attempts["n"] == 3  # PRD: max 3 attempts, then stop
