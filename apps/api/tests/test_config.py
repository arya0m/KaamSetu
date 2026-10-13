"""P1-foundation: settings come only from the environment."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings

REQUIRED = {"DATABASE_URL": "postgresql+psycopg://u:p@h/db", "REDIS_URL": "redis://h:6379/0"}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in ("DATABASE_URL", "REDIS_URL", "CELERY_BROKER_URL", "EMBEDDING_DIM"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # make sure no stray .env is picked up


@pytest.mark.usefixtures("clean_env")
def test_p1_connection_strings_have_no_defaults() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings()
    missing = {e["loc"][0] for e in exc.value.errors()}
    assert {"database_url", "redis_url"} <= missing


@pytest.mark.usefixtures("clean_env")
def test_p1_defaults_match_prd(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.embedding_dim == 768
    assert (s.match_weight_skill, s.match_weight_distance, s.match_weight_experience) == (
        0.6,
        0.25,
        0.15,
    )
    assert s.match_threshold == 0.55
    assert s.match_max_distance_km == 25.0
    assert s.broker_url == REQUIRED["REDIS_URL"]


@pytest.mark.usefixtures("clean_env")
def test_p1_env_overrides_and_separate_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("EMBEDDING_DIM", "1024")
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://broker:6379/1")
    s = Settings()
    assert s.embedding_dim == 1024
    assert s.broker_url == "redis://broker:6379/1"


@pytest.mark.usefixtures("clean_env")
def test_p1_match_weights_must_sum_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("MATCH_WEIGHT_SKILL", "0.9")
    with pytest.raises(ValidationError, match=r"sum to 1\.0"):
        Settings()
