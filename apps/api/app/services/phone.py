"""Phone number lookup hash and storage codec.

The hash (HMAC-SHA256 with a secret salt) is what we look workers up by. The
codec turns the number into `workers.wa_phone_encrypted` and back. Phase 2 uses
a plaintext codec by decision; Phase 7 swaps in real encryption, and production
refuses to start until it does (see Settings).
"""

import hashlib
import hmac
from typing import Protocol

from app.config import get_settings


def hash_phone(e164: str, salt: str | None = None) -> str:
    key = (salt if salt is not None else get_settings().phone_hash_salt).encode()
    return hmac.new(key, e164.encode(), hashlib.sha256).hexdigest()


class PhoneCodec(Protocol):
    def encode(self, e164: str) -> bytes: ...

    def decode(self, stored: bytes) -> str: ...


class PlaintextPhoneCodec:
    """Stores the number unencrypted. Replaced by encryption in Phase 7."""

    def encode(self, e164: str) -> bytes:
        return e164.encode()

    def decode(self, stored: bytes) -> str:
        return stored.decode()


def get_phone_codec() -> PhoneCodec:
    return PlaintextPhoneCodec()
