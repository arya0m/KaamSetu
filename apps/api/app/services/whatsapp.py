"""WhatsApp provider interface and the Twilio implementation.

Every provider call goes through `WhatsAppProvider`, so tests use a fake and a
Meta Cloud API provider can be added later without touching callers.
"""

import base64
import hashlib
import hmac
import logging
import time
from collections.abc import Sequence
from functools import lru_cache
from typing import Protocol

import httpx
import redis

from app.config import get_settings
from app.schemas.whatsapp import InboundMessage, StatusUpdate

logger = logging.getLogger(__name__)

Params = Sequence[tuple[str, str]]


class SendError(Exception):
    """Sending a WhatsApp message failed."""


class TransientSendError(SendError):
    """Worth retrying: rate limit, provider 5xx, network error."""


class PermanentSendError(SendError):
    """Not worth retrying: bad request, unknown recipient, auth failure."""


class MediaError(Exception):
    """A media file could not be downloaded."""


class TransientMediaError(MediaError):
    """Provider 5xx or network error while downloading: worth retrying."""


class WhatsAppProvider(Protocol):
    name: str

    def verify_signature(self, url: str, params: Params, signature: str | None) -> bool: ...

    def parse_inbound(self, params: Params) -> InboundMessage: ...

    def parse_status(self, params: Params) -> StatusUpdate: ...

    def send_text(self, to_phone: str, body: str, status_callback_url: str | None) -> str:
        """Send a text message; returns the provider's message ID."""
        ...

    def fetch_media(self, url: str, max_bytes: int) -> bytes:
        """Download an inbound media file (e.g. a voice note)."""
        ...


def twilio_signature(auth_token: str, url: str, params: Params) -> str:
    """X-Twilio-Signature: base64(HMAC-SHA1(token, url + sorted key/value pairs))."""
    payload = url
    for key in sorted({k for k, _ in params}):
        for value in sorted({v for k, v in params if k == key}):
            payload += key + value
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


class TwilioProvider:
    name = "twilio"
    PREFIX = "whatsapp:"

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        api_base_url: str = "https://api.twilio.com",
        timeout_s: float = 10.0,
        http: httpx.Client | None = None,
    ) -> None:
        self._sid = account_sid
        self._token = auth_token
        self._from = from_number
        self._base = api_base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=timeout_s)

    def verify_signature(self, url: str, params: Params, signature: str | None) -> bool:
        if not self._token or not signature:
            return False  # fail closed when unconfigured
        expected = twilio_signature(self._token, url, params)
        return hmac.compare_digest(expected, signature)

    @classmethod
    def _phone(cls, value: str) -> str:
        return value.removeprefix(cls.PREFIX)

    def parse_inbound(self, params: Params) -> InboundMessage:
        p = dict(params)
        body = p.get("Body", "").strip()
        media_count = int(p.get("NumMedia", "0") or 0)
        return InboundMessage(
            message_id=p.get("MessageSid", ""),
            from_phone=self._phone(p.get("From", "")),
            text=body or None,
            media_count=media_count,
            media_url=p.get("MediaUrl0") or None if media_count else None,
            media_type=p.get("MediaContentType0") or None if media_count else None,
        )

    def parse_status(self, params: Params) -> StatusUpdate:
        p = dict(params)
        return StatusUpdate.model_validate(
            {"message_id": p.get("MessageSid", ""), "status": p.get("MessageStatus", "")}
        )

    def send_text(self, to_phone: str, body: str, status_callback_url: str | None) -> str:
        data = {"From": self._from, "To": self.PREFIX + to_phone, "Body": body}
        if status_callback_url:
            data["StatusCallback"] = status_callback_url
        url = f"{self._base}/2010-04-01/Accounts/{self._sid}/Messages.json"
        try:
            resp = self._http.post(url, data=data, auth=(self._sid, self._token))
        except httpx.TransportError as exc:
            raise TransientSendError(f"twilio transport error: {type(exc).__name__}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientSendError(f"twilio HTTP {resp.status_code}")
        if resp.status_code >= 400:
            # Twilio error codes are safe to log; the request body (phone, text) is not.
            code = resp.json().get("code") if resp.content else None
            raise PermanentSendError(f"twilio HTTP {resp.status_code} code={code}")
        sid: str = resp.json()["sid"]
        return sid

    def fetch_media(self, url: str, max_bytes: int) -> bytes:
        """GET the media with account auth; Twilio redirects to a signed CDN URL.

        Only URLs under the configured Twilio API base (https://api.twilio.com by
        default) are fetched: the URL comes from the webhook, so this blocks
        server-side request forgery. The body size is capped.
        """
        if not url.startswith(self._base + "/"):
            raise MediaError("media URL is not a Twilio URL")
        try:
            with self._http.stream(
                "GET", url, auth=(self._sid, self._token), follow_redirects=True
            ) as resp:
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise TransientMediaError(f"twilio media HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    raise MediaError(f"twilio media HTTP {resp.status_code}")
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise MediaError("media file too large")
                    chunks.append(chunk)
        except httpx.TransportError as exc:
            raise TransientMediaError(
                f"twilio media transport error: {type(exc).__name__}"
            ) from exc
        return b"".join(chunks)


@lru_cache
def get_provider() -> WhatsAppProvider:
    s = get_settings()
    return TwilioProvider(
        account_sid=s.twilio_account_sid,
        auth_token=s.twilio_auth_token,
        from_number=s.twilio_whatsapp_from,
        api_base_url=s.twilio_api_base_url,
        timeout_s=s.whatsapp_send_timeout_s,
    )


class SendThrottle:
    """Global minimum gap between sends, shared across workers via Redis.

    The Twilio sandbox accepts one message every 3 seconds per number.
    """

    KEY = "wa:send:slot"

    def __init__(self, client: redis.Redis, interval_ms: int) -> None:
        self._redis = client
        self._interval_ms = interval_ms

    def acquire(self, timeout_s: float = 30.0, poll_s: float = 0.1) -> None:
        if self._interval_ms <= 0:
            return
        deadline = time.monotonic() + timeout_s
        while not self._redis.set(self.KEY, "1", nx=True, px=self._interval_ms):
            if time.monotonic() >= deadline:
                raise TransientSendError("send throttle: no slot within timeout")
            time.sleep(poll_s)
