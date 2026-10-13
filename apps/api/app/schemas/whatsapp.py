"""Provider-neutral WhatsApp message schemas."""

from typing import Literal

from pydantic import BaseModel, Field

E164 = r"^\+[1-9]\d{6,14}$"

DeliveryStatus = Literal[
    "accepted", "queued", "sending", "sent", "delivered", "read", "failed", "undelivered"
]  # fmt: skip


class InboundMessage(BaseModel):
    message_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    from_phone: str = Field(pattern=E164)
    text: str | None = Field(default=None, max_length=4096)
    media_count: int = Field(default=0, ge=0, le=10)
    media_url: str | None = Field(default=None, max_length=1024, pattern=r"^https://")
    media_type: str | None = Field(default=None, max_length=100)

    @property
    def is_audio(self) -> bool:
        return bool(self.media_url and (self.media_type or "").startswith("audio/"))

    @property
    def kind(self) -> str:
        if self.is_audio:
            return "audio"
        return "media" if self.media_count else "text"


class StatusUpdate(BaseModel):
    message_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    status: DeliveryStatus
