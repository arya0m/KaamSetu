"""P1-foundation: app wiring, OpenAPI docs, CORS, DB session, Celery."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import db
from app.config import get_settings


def test_p1_openapi_served_under_api_v1(client: TestClient) -> None:
    resp = client.get("/api/v1/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    assert spec["info"]["title"] == "KaamSetu API"
    assert {"/healthz", "/readyz"} <= set(spec["paths"])
    assert client.get("/api/v1/docs").status_code == 200


def test_p1_cors_allows_only_dashboard_origin(client: TestClient) -> None:
    allowed = get_settings().dashboard_origin
    ok = client.options(
        "/healthz", headers={"Origin": allowed, "Access-Control-Request-Method": "GET"}
    )
    assert ok.headers.get("access-control-allow-origin") == allowed

    evil = client.options(
        "/healthz",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in evil.headers


@pytest.fixture
def sqlite_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    get_settings.cache_clear()
    db.get_engine.cache_clear()
    db.get_sessionmaker.cache_clear()
    yield
    db.get_engine().dispose()
    get_settings.cache_clear()
    db.get_engine.cache_clear()
    db.get_sessionmaker.cache_clear()


@pytest.mark.usefixtures("sqlite_db")
def test_p1_get_session_yields_working_session_and_closes() -> None:
    gen = db.get_session()
    session = next(gen)
    assert session.execute(text("SELECT 1")).scalar_one() == 1
    with pytest.raises(StopIteration):
        next(gen)


def test_p1_celery_ping_task_runs() -> None:
    from app.workers.celery_app import celery_app
    from app.workers.tasks import ping

    assert "kaamsetu.ping" in celery_app.tasks
    assert ping.apply().get() == "pong"


def test_p1_celery_uses_redis_broker_from_settings() -> None:
    from app.workers.celery_app import celery_app

    assert celery_app.conf.broker_url == get_settings().broker_url
    assert celery_app.conf.task_serializer == "json"
