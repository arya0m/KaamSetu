"""P1-foundation: liveness and readiness probes."""

import os
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.readiness import ReadinessRegistry, default_registry


def _boom() -> None:
    raise ConnectionError("postgresql://secret-user:secret-pass@db-host/kaamsetu refused")


def test_p1_healthz_returns_ok(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_p1_readyz_green_when_all_checks_pass(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "checks": {"database": "ok", "pgvector": "ok", "redis": "ok"},
    }


def test_p1_readyz_503_uses_error_shape_and_hides_details(
    healthy_registry: ReadinessRegistry,
) -> None:
    healthy_registry.register("database", _boom)
    healthy_registry.register("redis", _boom)
    with TestClient(create_app(readiness=healthy_registry)) as c:
        resp = c.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == {"code": "not_ready", "message": "Unavailable: database, redis"}
    assert body["checks"] == {"database": "fail", "pgvector": "ok", "redis": "fail"}
    assert "secret" not in resp.text


def test_p1_readyz_check_timeout_counts_as_failure() -> None:
    registry = ReadinessRegistry(timeout_s=0.05)
    registry.register("slow", lambda: time.sleep(0.5))
    assert registry.run() == {"slow": "fail"}


def test_p1_readyz_hung_checks_share_one_deadline() -> None:
    registry = ReadinessRegistry(timeout_s=0.1)
    for name in ("a", "b", "c"):
        registry.register(name, lambda: time.sleep(1.0))
    registry.register("fast", lambda: None)
    started = time.monotonic()
    results = registry.run()
    assert time.monotonic() - started < 0.5
    assert results == {"a": "fail", "b": "fail", "c": "fail", "fast": "ok"}


def test_p1_readiness_registry_empty_is_ready() -> None:
    assert ReadinessRegistry(timeout_s=1.0).run() == {}


def test_p1_default_registry_checks_database_pgvector_redis() -> None:
    assert default_registry().names == ["database", "pgvector", "redis"]


@pytest.mark.skipif(bool(os.environ.get("TEST_DATABASE_URL")), reason="real services set")
def test_p1_default_checks_fail_when_services_unreachable() -> None:
    # conftest points DATABASE_URL/REDIS_URL at port 1 unless real test services are set.
    results = default_registry().run()
    assert results == {"database": "fail", "pgvector": "fail", "redis": "fail"}


def test_p3_provider_checks_registered_only_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings

    assert "llm" not in default_registry().names and "stt" not in default_registry().names
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("WATSON_STT_API_KEY", "w")
    monkeypatch.setenv("WATSON_STT_URL", "https://stt.example")
    get_settings.cache_clear()
    try:
        assert default_registry().names == ["database", "pgvector", "redis", "stt", "llm"]
        monkeypatch.setenv("LLM_PROVIDER", "watsonx")
        monkeypatch.setenv("WATSONX_API_KEY", "ibm")
        get_settings.cache_clear()
        assert "llm" in default_registry().names
    finally:
        get_settings.cache_clear()


def test_p3_provider_checks_are_cached_while_passing() -> None:
    from app.readiness import cached

    calls: list[int] = []
    now = [0.0]
    check = cached(lambda: calls.append(1), ttl_s=60, clock=lambda: now[0])
    check()
    check()
    assert len(calls) == 1
    now[0] = 61
    check()
    assert len(calls) == 2


def test_p3_failing_provider_check_is_not_cached() -> None:
    from app.readiness import cached

    calls: list[int] = []

    def down() -> None:
        calls.append(1)
        raise RuntimeError("401")

    check = cached(down, ttl_s=60)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            check()
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("fn", "env"),
    [
        ("check_openai", {"OPENAI_API_KEY": "sk"}),
        ("check_watsonx", {"WATSONX_API_KEY": "k"}),
        ("check_watson_stt", {"WATSON_STT_API_KEY": "k", "WATSON_STT_URL": "https://stt.example"}),
    ],
)
def test_p3_provider_checks_fail_on_http_errors(
    fn: str, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from app import readiness
    from app.config import get_settings

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    statuses = iter([200, 401])
    monkeypatch.setattr(httpx, "request", lambda *a, **k: httpx.Response(next(statuses)))
    try:
        getattr(readiness, fn)()  # 200 passes
        with pytest.raises(RuntimeError, match="401"):
            getattr(readiness, fn)()
    finally:
        get_settings.cache_clear()
