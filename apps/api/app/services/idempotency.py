"""Ignore duplicate webhook deliveries (PRD: Reliability) by provider message ID.

Redis is checked in the webhook before anything is queued, so duplicates of
pre-consent messages are caught without touching Postgres. For registered
workers, `messages.wa_message_id` is UNIQUE as a second line of defence.
"""

import redis

KEY_PREFIX = "wa:seen:"


def claim(client: redis.Redis, message_id: str, ttl_s: int) -> bool:
    """True the first time a message ID is seen within the TTL, False afterwards."""
    return bool(client.set(KEY_PREFIX + message_id, "1", nx=True, ex=ttl_s))


def release(client: redis.Redis, message_id: str) -> None:
    """Undo a claim when the message could not be queued, so the provider's retry is processed."""
    client.delete(KEY_PREFIX + message_id)
