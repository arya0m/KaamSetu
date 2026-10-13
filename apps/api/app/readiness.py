"""Pluggable readiness checks for /readyz.

Database, pgvector and Redis are always checked. Model providers (STT, LLM;
embeddings in Phase 5) are checked when configured, with results cached.
A check is a callable that returns normally when healthy and raises otherwise.
"""

import logging
import math
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any

import httpx
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


def cached(check: Check, ttl_s: float, clock: Callable[[], float] = time.monotonic) -> Check:
    """Remember a passing result for ttl_s so probes do not hammer paid APIs."""
    last_ok = [-math.inf]

    def run() -> None:
        if clock() - last_ok[0] < ttl_s:
            return
        check()
        last_ok[0] = clock()

    return run


def _http_ok(method: str, url: str, **kwargs: Any) -> None:
    resp = httpx.request(method, url, timeout=get_settings().readiness_timeout_s, **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")


def check_openai() -> None:
    s = get_settings()
    _http_ok("GET", f"{s.openai_base_url.rstrip('/')}/models",
             headers={"Authorization": f"Bearer {s.openai_api_key}"})  # fmt: skip


def check_watsonx() -> None:
    """A successful IAM token exchange proves the API key works."""
    s = get_settings()
    _http_ok(
        "POST",
        s.ibm_iam_url,
        data={"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": s.watsonx_api_key},
    )


def check_watson_stt() -> None:
    s = get_settings()
    _http_ok(
        "GET", f"{s.watson_stt_url.rstrip('/')}/v1/models", auth=("apikey", s.watson_stt_api_key)
    )


PROVIDER_CHECK_TTL_S = 60.0


def default_registry() -> ReadinessRegistry:
    s = get_settings()
    registry = ReadinessRegistry(timeout_s=s.readiness_timeout_s)
    registry.register("database", check_database)
    registry.register("pgvector", check_pgvector)
    registry.register("redis", check_redis)
    # Model providers (PRD: /readyz checks model providers). Registered only when
    # configured; production refuses to start without them (see Settings).
    if s.watson_stt_api_key and s.watson_stt_url:
        registry.register("stt", cached(check_watson_stt, PROVIDER_CHECK_TTL_S))
    if s.llm_provider == "openai" and s.openai_api_key:
        registry.register("llm", cached(check_openai, PROVIDER_CHECK_TTL_S))
    if s.llm_provider == "watsonx" and s.watsonx_api_key:
        registry.register("llm", cached(check_watsonx, PROVIDER_CHECK_TTL_S))
    return registry
