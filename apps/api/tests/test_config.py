"""P1-foundation: settings come only from the environment."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings

REQUIRED = {
    "DATABASE_URL": "postgresql+psycopg://u:p@h/db",
    "REDIS_URL": "redis://h:6379/0",
    "PHONE_HASH_SALT": "s" * 40,
}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in (
        "DATABASE_URL",
        "REDIS_URL",
        "CELERY_BROKER_URL",
        "EMBEDDING_DIM",
        "PHONE_HASH_SALT",
        "APP_ENV",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # make sure no stray .env is picked up


@pytest.mark.usefixtures("clean_env")
def test_p1_connection_strings_have_no_defaults() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings()
    missing = {e["loc"][0] for e in exc.value.errors()}
    assert {"database_url", "redis_url", "phone_hash_salt"} <= missing


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


PROD_OK = {
    "APP_ENV": "production",
    "DATABASE_URL": "postgresql+psycopg://kaamsetu:s3cr3t-from-vault@db/kaamsetu",
    "REDIS_URL": "redis://cache:6379/0",
    "DASHBOARD_ORIGIN": "https://dashboard.kaamsetu.example",
    "PUBLIC_BASE_URL": "https://api.kaamsetu.example",
    "PRIVACY_NOTICE_URL": "https://kaamsetu.example/privacy",
    "TWILIO_ACCOUNT_SID": "AC" + "1" * 32,
    "TWILIO_AUTH_TOKEN": "real-token-from-vault",
    "TWILIO_WHATSAPP_FROM": "whatsapp:+14155238886",
    "PHONE_HASH_SALT": "x" * 40,
}


def _problems(exc: pytest.ExceptionInfo[ValidationError]) -> str:
    return str(exc.value)


@pytest.mark.usefixtures("clean_env")
def test_p2_production_blocked_until_phone_encryption_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise-valid production settings fail only on the Phase 7 encryption gap."""
    for k, v in PROD_OK.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError, match="unsafe production settings") as exc:
        Settings()
    problems = _problems(exc)
    assert "unencrypted until Phase 7" in problems
    assert problems.count(";") == 0  # the only problem


@pytest.mark.usefixtures("clean_env")
@pytest.mark.parametrize(
    ("var", "value", "reason"),
    [
        ("DATABASE_URL", "postgresql+psycopg://kaamsetu:change-me@db/kaamsetu", "database_url"),
        ("REDIS_URL", "redis://:change-me@cache:6379/0", "redis_url"),
        ("CELERY_BROKER_URL", "redis://:change-me@cache:6379/1", "celery_broker_url"),
        ("TWILIO_AUTH_TOKEN", "change-me", "twilio_auth_token"),
        ("TWILIO_AUTH_TOKEN", "", "twilio_auth_token must be set"),
        ("TWILIO_ACCOUNT_SID", "", "twilio_account_sid must be set"),
        ("PHONE_HASH_SALT", "short", "phone_hash_salt must be at least 32"),
        ("DASHBOARD_ORIGIN", "http://dashboard.kaamsetu.example", "dashboard_origin"),
        ("PUBLIC_BASE_URL", "http://api.kaamsetu.example", "public_base_url"),
        ("PRIVACY_NOTICE_URL", "http://kaamsetu.example/privacy", "privacy_notice_url"),
    ],
)
def test_p1_production_rejects_unsafe_settings(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str, reason: str
) -> None:
    for k, v in PROD_OK.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv(var, value)
    with pytest.raises(ValidationError, match="unsafe production settings") as exc:
        Settings()
    assert reason in _problems(exc)


@pytest.mark.usefixtures("clean_env")
def test_p1_placeholder_allowed_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://kaamsetu:change-me@db/kaamsetu")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("PHONE_HASH_SALT", "dev")
    assert Settings().app_env != "production"


ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"


@pytest.mark.skipif(not ENV_EXAMPLE.exists(), reason="repo root not available")
def test_p1_every_setting_is_documented_in_env_example() -> None:
    documented = {
        line.split("=", 1)[0].strip().lower()
        for line in ENV_EXAMPLE.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert set(Settings.model_fields) <= documented
