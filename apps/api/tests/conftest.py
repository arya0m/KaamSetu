"""Shared fixtures.

Unit tests need no services: settings point at unreachable hosts and readiness
checks are replaced with fakes. Tests marked `db` run against a real
PostgreSQL+pgvector and Redis given by TEST_DATABASE_URL / TEST_REDIS_URL
(CI provides both) and are skipped otherwise, or fail if REQUIRE_DB_TESTS=1.
"""

import os
from collections.abc import Iterator

import pytest

# Must happen before any `app` import: settings are read at import time.
os.environ.setdefault("APP_ENV", "test")
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://unused:unused@127.0.0.1:1/unused"
)
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:1/0")

from fastapi.testclient import TestClient

from app.main import create_app
from app.readiness import ReadinessRegistry


def _ok() -> None:
    return None


@pytest.fixture
def healthy_registry() -> ReadinessRegistry:
    registry = ReadinessRegistry(timeout_s=1.0)
    for name in ("database", "pgvector", "redis"):
        registry.register(name, _ok)
    return registry


@pytest.fixture
def client(healthy_registry: ReadinessRegistry) -> Iterator[TestClient]:
    with TestClient(create_app(readiness=healthy_registry), raise_server_exceptions=False) as c:
        yield c


def _service_url(var: str) -> str:
    url = os.environ.get(var)
    if not url:
        if os.environ.get("REQUIRE_DB_TESTS") == "1":
            pytest.fail(f"{var} must be set when REQUIRE_DB_TESTS=1")
        pytest.skip(f"{var} not set")
    return url


@pytest.fixture
def database_url() -> str:
    return _service_url("TEST_DATABASE_URL")


@pytest.fixture
def redis_url() -> str:
    return _service_url("TEST_REDIS_URL")
