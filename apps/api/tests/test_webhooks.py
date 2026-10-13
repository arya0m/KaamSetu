"""Phase 2 webhook: signature check, idempotency, queueing, fast 200 (PRD: Reliability)."""

import time
from typing import Any

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.workers import tasks
from tests.conftest import FakeWhatsApp, signed_post, twilio_inbound

PATH = "/webhooks/whatsapp"
PHONE = "+919876543210"


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(tasks.handle_inbound, "delay", lambda m: calls.append(m))
    return calls


@pytest.fixture
def statuses(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(tasks.update_message_status, "delay", lambda s: calls.append(s))
    return calls


@pytest.mark.usefixtures("fake_redis", "fake_whatsapp")
def test_p2_signed_message_is_queued_and_acknowledged(
    client: TestClient, queued: list[dict[str, Any]]
) -> None:
    resp = signed_post(client, PATH, twilio_inbound("SM1", PHONE, "hello"))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/xml")
    assert "<Response>" in resp.text
    assert queued == [
        {
            "message_id": "SM1",
            "from_phone": PHONE,
            "text": "hello",
            "media_count": 0,
            "media_url": None,
            "media_type": None,
        }
    ]


@pytest.mark.usefixtures("fake_redis", "fake_whatsapp")
def test_p2_missing_signature_is_rejected(client: TestClient, queued: list[Any]) -> None:
    from urllib.parse import urlencode

    resp = client.post(
        PATH,
        content=urlencode(twilio_inbound("SM1", PHONE, "hi")),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "invalid_signature"
    assert queued == []


@pytest.mark.usefixtures("fake_redis", "fake_whatsapp")
def test_p2_wrong_secret_is_rejected(client: TestClient, queued: list[Any]) -> None:
    resp = signed_post(client, PATH, twilio_inbound("SM1", PHONE, "hi"), token="wrong")
    assert resp.status_code == 403
    assert queued == []


@pytest.mark.usefixtures("fake_redis", "fake_whatsapp")
def test_p2_duplicate_delivery_is_queued_once(client: TestClient, queued: list[Any]) -> None:
    for _ in range(3):
        assert signed_post(client, PATH, twilio_inbound("SM1", PHONE, "hi")).status_code == 200
    assert len(queued) == 1


@pytest.mark.usefixtures("fake_whatsapp")
def test_p2_queue_failure_returns_503_and_allows_retry(
    client: TestClient, fake_redis: fakeredis.FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_: Any) -> None:
        raise ConnectionError("broker down")

    monkeypatch.setattr(tasks.handle_inbound, "delay", broken)
    resp = signed_post(client, PATH, twilio_inbound("SM1", PHONE, "hi"))
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "unavailable"
    assert fake_redis.get("wa:seen:SM1") is None  # claim released, Twilio's retry will go through


@pytest.mark.usefixtures("fake_redis", "fake_whatsapp")
def test_p2_signed_but_malformed_message_is_acknowledged_not_queued(
    client: TestClient, queued: list[Any]
) -> None:
    params = twilio_inbound("SM1", "not-a-phone", "hi")
    params["From"] = "whatsapp:12345"
    assert signed_post(client, PATH, params).status_code == 200
    assert queued == []


@pytest.mark.usefixtures("fake_redis", "fake_whatsapp")
def test_p2_oversized_body_is_rejected(client: TestClient, queued: list[Any]) -> None:
    resp = signed_post(client, PATH, twilio_inbound("SM1", PHONE, "x" * 70_000))
    assert resp.status_code == 413
    assert queued == []


@pytest.mark.usefixtures("fake_redis", "fake_whatsapp")
def test_p2_get_is_not_supported_with_twilio(client: TestClient) -> None:
    assert client.get(PATH).status_code == 405


@pytest.mark.usefixtures("fake_redis", "fake_whatsapp")
def test_p2_webhook_returns_well_under_two_seconds(client: TestClient, queued: list[Any]) -> None:
    start = time.perf_counter()
    for i in range(20):
        signed_post(client, PATH, twilio_inbound(f"SM{i}", PHONE, "hi"))
    assert (time.perf_counter() - start) / 20 < 0.2
    assert len(queued) == 20


@pytest.mark.usefixtures("fake_redis", "fake_whatsapp")
def test_p2_status_callback_is_queued(client: TestClient, statuses: list[Any]) -> None:
    resp = signed_post(
        client, PATH + "/status", {"MessageSid": "SM9", "MessageStatus": "delivered"}
    )
    assert resp.status_code == 200
    assert statuses == [{"message_id": "SM9", "status": "delivered"}]


@pytest.mark.usefixtures("fake_redis", "fake_whatsapp")
def test_p2_status_callback_requires_signature(client: TestClient, statuses: list[Any]) -> None:
    resp = signed_post(
        client, PATH + "/status", {"MessageSid": "SM9", "MessageStatus": "read"}, token="x"
    )
    assert resp.status_code == 403
    assert statuses == []


@pytest.mark.usefixtures("fake_redis", "fake_whatsapp")
def test_p2_unknown_status_is_acknowledged_and_ignored(
    client: TestClient, statuses: list[Any]
) -> None:
    resp = signed_post(client, PATH + "/status", {"MessageSid": "SM9", "MessageStatus": "weird"})
    assert resp.status_code == 200
    assert statuses == []


def test_p2_fake_provider_verifies_like_twilio(fake_whatsapp: FakeWhatsApp) -> None:
    assert fake_whatsapp.verify_signature("https://x", [], None) is False
