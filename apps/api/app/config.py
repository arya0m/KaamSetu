"""Application settings, loaded only from environment variables (or a local .env)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The password used in .env.example; production refuses to start with it.
PLACEHOLDER_SECRET = "change-me"  # noqa: S105  # a deny-list marker, not a credential


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # Connection strings carry credentials, so they have no defaults.
    database_url: str
    redis_url: str
    celery_broker_url: str | None = None

    # CORS is limited to the dashboard origin (PRD: Security).
    dashboard_origin: str = "http://localhost:3000"

    # Must match the embedding model; read by models and the initial migration.
    embedding_dim: int = Field(default=768, gt=0)

    # Match scoring (PRD: AI pipeline, step 4). Tunable against the test set.
    match_weight_skill: float = Field(default=0.6, ge=0, le=1)
    match_weight_distance: float = Field(default=0.25, ge=0, le=1)
    match_weight_experience: float = Field(default=0.15, ge=0, le=1)
    match_threshold: float = Field(default=0.55, ge=0, le=1)
    match_max_distance_km: float = Field(default=25.0, gt=0)

    # Connection pool. A hung database must not pin request threads forever.
    db_connect_timeout_s: int = Field(default=5, gt=0)
    db_pool_size: int = Field(default=5, gt=0)
    db_max_overflow: int = Field(default=10, ge=0)

    readiness_timeout_s: float = Field(default=2.0, gt=0)

    # --- WhatsApp (Twilio; PRD open question resolved: Twilio sandbox) -----
    whatsapp_provider: Literal["twilio"] = "twilio"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""  # also the webhook signing key; empty rejects every webhook
    twilio_whatsapp_from: str = ""  # e.g. whatsapp:+14155238886 for the sandbox
    twilio_api_base_url: str = "https://api.twilio.com"
    # The exact public URL Twilio calls; webhook signatures are computed over it.
    public_base_url: str = "http://localhost:8000"
    # The Twilio sandbox allows one message per 3 s; 0 disables throttling.
    whatsapp_min_send_interval_ms: int = Field(default=0, ge=0)
    whatsapp_send_timeout_s: float = Field(default=10.0, gt=0)

    # --- Onboarding and consent (FR-1) --------------------------------------
    privacy_notice_url: str = "http://localhost:3000/privacy"
    # Pre-consent state (step, chosen language) lives only in Redis, then expires.
    preconsent_ttl_s: int = Field(default=86_400, gt=0)
    # Webhook de-duplication window for provider message IDs.
    idempotency_ttl_s: int = Field(default=7 * 86_400, gt=0)

    # --- Phone numbers ------------------------------------------------------
    # Secret salt for the lookup hash (HMAC-SHA256). No default: it is a secret.
    phone_hash_salt: str
    # "none" stores the number as plaintext until Phase 7 adds encryption.
    # Production refuses to start with "none".
    phone_encryption: Literal["none"] = "none"

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "Settings":
        total = self.match_weight_skill + self.match_weight_distance + self.match_weight_experience
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"match weights must sum to 1.0, got {total:.4f}")
        return self

    @model_validator(mode="after")
    def _production_is_safe(self) -> "Settings":
        """Refuse to boot production with example credentials or insecure origins."""
        if self.app_env != "production":
            return self
        problems = [
            f"{name} still contains the .env.example placeholder"
            for name in (
                "database_url",
                "redis_url",
                "celery_broker_url",
                "twilio_auth_token",
                "phone_hash_salt",
            )
            if PLACEHOLDER_SECRET in (getattr(self, name) or "")
        ]
        for name in ("dashboard_origin", "public_base_url", "privacy_notice_url"):
            if not getattr(self, name).startswith("https://"):
                problems.append(f"{name} must use https:// in production")
        for name in ("twilio_account_sid", "twilio_auth_token", "twilio_whatsapp_from"):
            if not getattr(self, name):
                problems.append(f"{name} must be set in production")
        if len(self.phone_hash_salt) < 32:
            problems.append("phone_hash_salt must be at least 32 characters in production")
        if self.phone_encryption == "none":
            problems.append("phone numbers are stored unencrypted until Phase 7 adds encryption")
        if problems:
            raise ValueError("unsafe production settings: " + "; ".join(problems))
        return self

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
