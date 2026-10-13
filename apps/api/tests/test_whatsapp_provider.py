"""Phase 2: Twilio provider — signatures, parsing, sending, retries classification, throttle."""

import time
from urllib.parse import parse_qs

import fakeredis
import httpx
import pytest
from pydantic import ValidationError
from twilio.request_validator import RequestValidator

from app.services.whatsapp import (
    PermanentSendError,
    SendThrottle,
    TransientSendError,
    TwilioProvider,
    twilio_signature,
)

TOKEN = "12345"
URL = "https://api.kaamsetu.test/webhooks/whatsapp"
PARAMS = [
    ("MessageSid", "SM" + "a" * 32),
    ("From", "whatsapp:+919876543210"),
    ("Body", "नमस्ते & hello=1"),
    ("NumMedia", "0"),
    ("Empty", ""),
]


def _provider(handler: httpx.MockTransport | None = None) -> TwilioProvider:
    http = httpx.Client(transport=handler) if handler else None
    return TwilioProvider("AC" + "0" * 32, TOKEN, "whatsapp:+14155238886", http=http)


def test_p2_signature_matches_official_twilio_sdk() -> None:
    expected = RequestValidator(TOKEN).compute_signature(URL, dict(PARAMS))
    assert twilio_signature(TOKEN, URL, PARAMS) == expected


def test_p2_valid_signature_accepted() -> None:
    sig = RequestValidator(TOKEN).compute_signature(URL, dict(PARAMS))
    assert _provider().verify_signature(URL, PARAMS, sig) is True


@pytest.mark.parametrize(
    ("url", "params", "signature"),
    [
        (URL, PARAMS, None),
        (URL, PARAMS, ""),
        (URL, PARAMS, "bm90LXRoZS1zaWduYXR1cmU="),
        (URL + "?x=1", PARAMS, "SIG"),  # URL changed after signing
        (URL, [*PARAMS[:-1], ("Body", "tampered")], "SIG"),  # body changed
    ],
)
def test_p2_bad_signature_rejected(
    url: str, params: list[tuple[str, str]], signature: str | None
) -> None:
    good = twilio_signature(TOKEN, URL, PARAMS)
    sig = good if signature == "SIG" else signature
    assert _provider().verify_signature(url, params, sig) is False


def test_p2_unconfigured_token_rejects_everything() -> None:
    provider = TwilioProvider("AC", "", "whatsapp:+1")
    assert provider.verify_signature(URL, PARAMS, twilio_signature("", URL, PARAMS)) is False


def test_p2_parse_inbound_text() -> None:
    msg = _provider().parse_inbound(PARAMS)
    assert msg.message_id == "SM" + "a" * 32
    assert msg.from_phone == "+919876543210"
    assert msg.text == "नमस्ते & hello=1"
    assert msg.kind == "text"


def test_p2_parse_inbound_media_without_body() -> None:
    params = [("MessageSid", "SM1"), ("From", "whatsapp:+919876543210"), ("NumMedia", "1")]
    msg = _provider().parse_inbound(params)
    assert msg.text is None
    assert msg.media_count == 1
    assert msg.kind == "media"


@pytest.mark.parametrize(
    "params",
    [
        [("MessageSid", "SM1"), ("From", "whatsapp:9876543210")],  # not E.164
        [("MessageSid", ""), ("From", "whatsapp:+919876543210")],
        [("MessageSid", "SM1; DROP TABLE"), ("From", "whatsapp:+919876543210")],
    ],
)
def test_p2_parse_inbound_rejects_bad_input(params: list[tuple[str, str]]) -> None:
    with pytest.raises(ValidationError):
        _provider().parse_inbound(params)


def test_p2_parse_status() -> None:
    status = _provider().parse_status([("MessageSid", "SM1"), ("MessageStatus", "delivered")])
    assert (status.message_id, status.status) == ("SM1", "delivered")
    with pytest.raises(ValidationError):
        _provider().parse_status([("MessageSid", "SM1"), ("MessageStatus", "exploded")])


def test_p2_send_text_posts_exact_twilio_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"sid": "SM" + "b" * 32})

    sid = _provider(httpx.MockTransport(handler)).send_text(
        "+919876543210", "नमस्ते", "https://api.kaamsetu.test/webhooks/whatsapp/status"
    )
    assert sid == "SM" + "b" * 32
    (req,) = seen
    assert req.url == f"https://api.twilio.com/2010-04-01/Accounts/AC{'0' * 32}/Messages.json"
    assert req.headers["authorization"].startswith("Basic ")
    form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
    assert form == {
        "From": "whatsapp:+14155238886",
        "To": "whatsapp:+919876543210",
        "Body": "नमस्ते",
        "StatusCallback": "https://api.kaamsetu.test/webhooks/whatsapp/status",
    }


@pytest.mark.parametrize("status", [429, 500, 503])
def test_p2_send_retryable_errors_are_transient(status: int) -> None:
    provider = _provider(httpx.MockTransport(lambda _: httpx.Response(status)))
    with pytest.raises(TransientSendError):
        provider.send_text("+919876543210", "x", None)


def test_p2_send_network_error_is_transient() -> None:
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(TransientSendError):
        _provider(httpx.MockTransport(boom)).send_text("+919876543210", "x", None)


def test_p2_send_client_error_is_permanent_and_hides_body() -> None:
    provider = _provider(
        httpx.MockTransport(lambda _: httpx.Response(400, json={"code": 21211, "message": "x"}))
    )
    with pytest.raises(PermanentSendError) as exc:
        provider.send_text("+919876543210", "secret text", None)
    assert "21211" in str(exc.value)
    assert "secret text" not in str(exc.value) and "+91" not in str(exc.value)


def test_p2_throttle_spaces_sends() -> None:
    throttle = SendThrottle(fakeredis.FakeRedis(decode_responses=True), interval_ms=150)
    start = time.monotonic()
    throttle.acquire(poll_s=0.01)
    throttle.acquire(poll_s=0.01)
    assert time.monotonic() - start >= 0.14


def test_p2_throttle_times_out_as_transient() -> None:
    throttle = SendThrottle(fakeredis.FakeRedis(decode_responses=True), interval_ms=10_000)
    throttle.acquire()
    with pytest.raises(TransientSendError):
        throttle.acquire(timeout_s=0.05, poll_s=0.01)


def test_p2_throttle_disabled_at_zero() -> None:
    throttle = SendThrottle(fakeredis.FakeRedis(decode_responses=True), interval_ms=0)
    for _ in range(5):
        throttle.acquire(timeout_s=0)
