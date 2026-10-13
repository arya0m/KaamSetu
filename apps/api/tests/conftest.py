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

import fakeredis
import httpx
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine

from app.main import create_app
from app.readiness import ReadinessRegistry
from app.routers import webhooks
from app.schemas.whatsapp import InboundMessage, StatusUpdate
from app.services.whatsapp import Params, TwilioProvider, twilio_signature
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


def twilio_inbound(sid: str, phone: str, body: str = "", num_media: int = 0) -> dict[str, str]:
    return {
        "MessageSid": sid,
        "SmsMessageSid": sid,
        "AccountSid": "AC" + "0" * 32,
        "From": f"whatsapp:{phone}",
        "To": "whatsapp:+14155238886",
        "Body": body,
        "NumMedia": str(num_media),
        "ProfileName": "Test Worker",
        "WaId": phone.lstrip("+"),
    }
