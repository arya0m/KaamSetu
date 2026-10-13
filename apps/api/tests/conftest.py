"""Shared fixtures.

Unit tests need no services: settings point at unreachable hosts, Redis is
fakeredis, WhatsApp is a fake provider and readiness checks are fakes. Tests
marked `db` run against a real PostgreSQL+pgvector and Redis given by
TEST_DATABASE_URL / TEST_REDIS_URL (CI provides both) and are skipped
otherwise, or fail if REQUIRE_DB_TESTS=1.
"""

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import pytest

# Must happen before any `app` import: settings are read at import time.
os.environ.setdefault("APP_ENV", "test")
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://unused:unused@127.0.0.1:1/unused"
)
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:1/0")
os.environ["PHONE_HASH_SALT"] = "test-salt-not-a-secret-0123456789abcdef"
os.environ["TWILIO_ACCOUNT_SID"] = "AC" + "0" * 32
os.environ["TWILIO_AUTH_TOKEN"] = "test-auth-token"
os.environ["TWILIO_WHATSAPP_FROM"] = "whatsapp:+14155238886"
os.environ["PUBLIC_BASE_URL"] = "https://api.kaamsetu.test"
os.environ["PRIVACY_NOTICE_URL"] = "https://kaamsetu.test/privacy"
os.environ["WHATSAPP_MIN_SEND_INTERVAL_MS"] = "0"

from itertools import count

import fakeredis
import httpx
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session

from app.db import get_engine, get_sessionmaker
from app.main import create_app
from app.models import Worker
from app.readiness import ReadinessRegistry
from app.routers import webhooks
from app.schemas.whatsapp import InboundMessage, StatusUpdate
from app.services.phone import hash_phone
from app.services.whatsapp import Params, TwilioProvider, twilio_signature

if TYPE_CHECKING:
    from app.ai.llm import LLMResponse
from app.workers import tasks
from app.workers.celery_app import celery_app
from tests.support import alembic_config

AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"]


def _ok() -> None:
    return None


@pytest.fixture
def healthy_registry() -> ReadinessRegistry:
    registry = ReadinessRegistry(timeout_s=1.0)
    for name in ("database", "pgvector", "redis"):
        registry.register(name, _ok)
    return registry


@pytest.fixture
def client(healthy_registry: ReadinessRegistry) -> Iterator[TestClient]:
    with TestClient(create_app(readiness=healthy_registry), raise_server_exceptions=False) as c:
        yield c


def _service_url(var: str) -> str:
    url = os.environ.get(var)
    if not url:
        if os.environ.get("REQUIRE_DB_TESTS") == "1":
            pytest.fail(f"{var} must be set when REQUIRE_DB_TESTS=1")
        pytest.skip(f"{var} not set")
    return url


@pytest.fixture
def database_url() -> str:
    return _service_url("TEST_DATABASE_URL")


@pytest.fixture
def redis_url() -> str:
    return _service_url("TEST_REDIS_URL")


@pytest.fixture
def migrated(database_url: str) -> Iterator[Engine]:
    """A freshly migrated test database, emptied again afterwards."""
    cfg = alembic_config(database_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine = create_engine(database_url)
    yield engine
    engine.dispose()
    command.downgrade(cfg, "base")


# --- WhatsApp fakes ----------------------------------------------------------


@dataclass
class FakeWhatsApp:
    """Records sends; parses and verifies exactly like the real Twilio provider."""

    sent: list[tuple[str, str]] = field(default_factory=list)
    name: str = "fake"
    _real: TwilioProvider = field(
        default_factory=lambda: TwilioProvider("AC" + "0" * 32, AUTH_TOKEN, "whatsapp:+1")
    )

    def verify_signature(self, url: str, params: Params, signature: str | None) -> bool:
        return self._real.verify_signature(url, params, signature)

    def parse_inbound(self, params: Params) -> InboundMessage:
        return self._real.parse_inbound(params)

    def parse_status(self, params: Params) -> StatusUpdate:
        return self._real.parse_status(params)

    def send_text(self, to_phone: str, body: str, status_callback_url: str | None) -> str:
        self.sent.append((to_phone, body))
        return f"SMOUT{len(self.sent):027d}"  # never collides with inbound test IDs

    media: dict[str, bytes] = field(default_factory=dict)

    def fetch_media(self, url: str, max_bytes: int) -> bytes:
        from app.services.whatsapp import MediaError

        if url not in self.media:
            raise MediaError("not found")
        return self.media[url]

    def bodies_to(self, phone: str) -> list[str]:
        return [body for to, body in self.sent if to == phone]


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> "fakeredis.FakeRedis":
    r = fakeredis.FakeRedis(decode_responses=True)
    for module in (webhooks, tasks):
        monkeypatch.setattr(module, "get_redis", lambda: r)
    return r


@pytest.fixture
def fake_whatsapp(monkeypatch: pytest.MonkeyPatch) -> FakeWhatsApp:
    fake = FakeWhatsApp()
    for module in (webhooks, tasks):
        monkeypatch.setattr(module, "get_provider", lambda: fake)
    return fake


@pytest.fixture
def eager_celery() -> Iterator[None]:
    """Run .delay() inline so webhook → task → send happens in one test."""
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    yield
    celery_app.conf.task_always_eager = False
    celery_app.conf.task_eager_propagates = False


def signed_post(
    client: TestClient, path: str, params: dict[str, str], token: str = AUTH_TOKEN
) -> httpx.Response:
    """POST form params the way Twilio does, with a valid X-Twilio-Signature."""
    signature = twilio_signature(token, PUBLIC_BASE_URL + path, list(params.items()))
    response: httpx.Response = client.post(
        path,
        content=urlencode(params),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Twilio-Signature": signature,
        },
    )
    return response


def twilio_inbound(
    sid: str, phone: str, body: str = "", num_media: int = 0, media_url: str | None = None
) -> dict[str, str]:
    media = {"MediaUrl0": media_url, "MediaContentType0": "audio/ogg"} if media_url else {}
    return {
        **media,
        "MessageSid": sid,
        "SmsMessageSid": sid,
        "AccountSid": "AC" + "0" * 32,
        "From": f"whatsapp:{phone}",
        "To": "whatsapp:+14155238886",
        "Body": body,
        "NumMedia": str(num_media or (1 if media_url else 0)),
        "ProfileName": "Test Worker",
        "WaId": phone.lstrip("+"),
    }


# --- AI fakes (FR-2 / FR-3) ----------------------------------------------------


def profile_json(**overrides: object) -> str:
    """A valid LLM extraction reply; evidence must quote the transcript."""
    import json

    data: dict[str, object] = {
        "name": None,
        "primary_trade": "electrician",
        "skills": [{"name": "house wiring", "level": "expert", "evidence": "wiring"}],
        "years_experience": 8,
        "location": {"city": "Delhi", "area": "Laxmi Nagar", "pincode": None},
        "languages_spoken": ["Hindi"],
        "availability": "full_time",
        "expected_daily_pay_inr": 800,
        "missing_fields": [],
        "confidence": 0.9,
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


@dataclass
class FakeLLM:
    """Returns scripted replies in order (the last one repeats) and records prompts."""

    replies: list[str] = field(default_factory=lambda: [profile_json()])
    calls: list[list[dict[str, str]]] = field(default_factory=list)
    provider: str = "fake"
    model: str = "fake-1"

    def complete_json(
        self, messages: list[dict[str, str]], schema: dict[str, object], schema_name: str
    ) -> "LLMResponse":
        from app.ai.llm import LLMResponse

        self.calls.append(messages)
        reply = self.replies[min(len(self.calls), len(self.replies)) - 1]
        return LLMResponse(reply, 100, 50)

    def user_payload(self, call: int = -1) -> dict[str, object]:
        import json

        payload: dict[str, object] = json.loads(self.calls[call][1]["content"])
        return payload


@dataclass
class FakeSTT:
    """Watson's language coverage; transcripts are scripted."""

    transcripts: list[str] = field(default_factory=lambda: ["mai bijli ka kaam karta hoon, wiring"])
    provider: str = "fake-stt"
    calls: int = 0

    def supports(self, language: str) -> bool:
        return language in ("hi", "en")

    def transcribe(self, wav: bytes, language: str) -> tuple[str, float | None]:
        self.calls += 1
        return self.transcripts[min(self.calls, len(self.transcripts)) - 1], 0.9


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    from app.services import inbound

    llm = FakeLLM()
    monkeypatch.setattr(inbound, "get_llm", lambda: llm)
    return llm


@pytest.fixture
def fake_stt(monkeypatch: pytest.MonkeyPatch) -> FakeSTT:
    """Fake STT plus a fake ffmpeg step, so flow tests do not need ffmpeg."""
    from app.ai.stt import AudioTooLong, Transcript
    from app.services import inbound

    stt = FakeSTT()
    monkeypatch.setattr(inbound, "get_stt", lambda: stt)

    def fake_transcribe(engine: FakeSTT, audio: bytes, language: str) -> Transcript:
        if audio == b"TOO-LONG":
            raise AudioTooLong(150.0)
        text, confidence = engine.transcribe(b"wav", language)
        return Transcript(text, language, 12.0, confidence)

    monkeypatch.setattr(inbound, "transcribe_audio", fake_transcribe)
    return stt


# --- end-to-end loop over the webhook (db tests) ---------------------------------

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
    fake_llm: FakeLLM,
    fake_stt: FakeSTT,
    eager_celery: None,
) -> Iterator["Loop"]:
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    yield Loop(client, fake_whatsapp, fake_redis, migrated, fake_llm, fake_stt)
    get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


class Loop:
    def __init__(
        self,
        client: TestClient,
        wa: FakeWhatsApp,
        r: fakeredis.FakeRedis,
        engine: Engine,
        llm: "FakeLLM",
        stt: "FakeSTT",
    ) -> None:
        self.client, self.wa, self.redis, self.engine = client, wa, r, engine
        self.llm, self.stt = llm, stt
        self._ids = count(1)

    def send(
        self,
        phone: str,
        body: str = "",
        num_media: int = 0,
        sid: str | None = None,
        media_url: str | None = None,
    ) -> str:
        sid = sid or f"SM{next(self._ids):032d}"
        params = twilio_inbound(sid, phone, body, num_media, media_url)
        resp = signed_post(self.client, "/webhooks/whatsapp", params)
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
