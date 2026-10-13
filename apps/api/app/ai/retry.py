"""Retries with exponential backoff (PRD: STT, LLM and send failures; max 3 attempts)."""

import logging
import random
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


def with_retries[T](
    fn: Callable[[], T],
    *,
    retry_on: tuple[type[BaseException], ...],
    attempts: int = 3,
    base_delay_s: float = 0.5,
    max_delay_s: float = 4.0,
    sleep: Callable[[float], None] = time.sleep,
    label: str = "call",
) -> T:
    """Call `fn`, retrying `retry_on` errors with jittered exponential backoff."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            if attempt == attempts:
                raise
            delay = min(max_delay_s, base_delay_s * 2 ** (attempt - 1))
            delay *= 0.5 + random.random() / 2  # noqa: S311  # jitter, not crypto
            logger.warning(
                "%s failed (attempt %d/%d, %s); retrying in %.2fs",
                label, attempt, attempts, type(exc).__name__, delay,
            )  # fmt: skip
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
