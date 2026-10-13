"""Pluggable readiness checks for /readyz.

Phase 1 registers database, pgvector and Redis. Later phases register their
model providers (STT, LLM, embeddings) with `ReadinessRegistry.register`.
A check is a callable that returns normally when healthy and raises otherwise.
"""

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait

import redis
from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine

logger = logging.getLogger(__name__)

Check = Callable[[], None]


class ReadinessRegistry:
    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s
        self._checks: dict[str, Check] = {}

    def register(self, name: str, check: Check) -> None:
        self._checks[name] = check

    @property
    def names(self) -> list[str]:
        return list(self._checks)

    def run(self) -> dict[str, str]:
        """Run all checks concurrently within one shared deadline.

        Returns name -> "ok" | "fail". A hung check never delays the response
        beyond `timeout_s`: the pool is shut down without waiting for it.
        """
        if not self._checks:
            return {}
        pool = ThreadPoolExecutor(max_workers=len(self._checks), thread_name_prefix="readyz")
        futures = {name: pool.submit(check) for name, check in self._checks.items()}
        wait(futures.values(), timeout=self.timeout_s)
        pool.shutdown(wait=False, cancel_futures=True)

        results: dict[str, str] = {}
        for name, future in futures.items():
            if not future.done():
                logger.warning("readiness check %s timed out", name)
                results[name] = "fail"
            elif (exc := future.exception()) is not None:
                # One line, no traceback (probes poll often); responses never include details.
                logger.warning("readiness check %s failed: %s", name, type(exc).__name__)
                results[name] = "fail"
            else:
                results[name] = "ok"
        return results


def check_database() -> None:
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))


def check_pgvector() -> None:
    with get_engine().connect() as conn:
        found = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).first()
    if found is None:
        raise RuntimeError("pgvector extension is not installed; run migrations")


def check_redis() -> None:
    timeout = get_settings().readiness_timeout_s
    client = redis.Redis.from_url(
        get_settings().redis_url, socket_timeout=timeout, socket_connect_timeout=timeout
    )
    try:
        client.ping()
    finally:
        client.close()


def default_registry() -> ReadinessRegistry:
    registry = ReadinessRegistry(timeout_s=get_settings().readiness_timeout_s)
    registry.register("database", check_database)
    registry.register("pgvector", check_pgvector)
    registry.register("redis", check_redis)
    return registry
