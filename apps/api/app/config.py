"""Application settings, loaded only from environment variables (or a local .env)."""

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The password used in .env.example; production refuses to start with it.
PLACEHOLDER_SECRET = "change-me"  # noqa: S105  # a deny-list marker, not a credential


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # staging: a public demo deploy. It gets production's secret and HTTPS checks but
    # may run DEMO_MODE and the Phase 7 gaps (unencrypted phones). See CLAUDE.md.
    app_env: Literal["development", "test", "staging", "production"] = "development"
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
    # On Render it defaults to the service's RENDER_EXTERNAL_URL.
    public_base_url: str = Field(
        default="http://localhost:8000",
        validation_alias=AliasChoices("PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL"),
    )
    # The Twilio sandbox allows one message per 3 s; 0 disables throttling.
    whatsapp_min_send_interval_ms: int = Field(default=0, ge=0)
    whatsapp_send_timeout_s: float = Field(default=10.0, gt=0)

    # --- Onboarding and consent (FR-1) --------------------------------------
    # Empty means this API's own plain-language notice at <public_base_url>/privacy.
    privacy_notice_url: str = ""
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

    # --- Speech to text (FR-2). Watson covers Hindi and Indian English only;
    # workers in other languages are asked to type (user decision, Phase 3).
    stt_provider: Literal["watson"] = "watson"
    watson_stt_api_key: str = ""
    watson_stt_url: str = ""  # instance URL from the service credentials
    voice_max_seconds: int = Field(default=120, gt=0)  # FR-2: up to 2 minutes
    media_max_bytes: int = Field(default=16 * 1024 * 1024, gt=0)
    ffmpeg_timeout_s: float = Field(default=30.0, gt=0)

    # --- LLM extraction (FR-3). No watsonx credits yet, so OpenAI is default.
    llm_provider: Literal["openai", "watsonx"] = "openai"
    llm_timeout_s: float = Field(default=30.0, gt=0)
    llm_max_output_tokens: int = Field(default=1500, gt=0)
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-6-luna"
    openai_reasoning_effort: str = "none"  # empty omits the parameter
    watsonx_api_key: str = ""
    watsonx_project_id: str = ""
    watsonx_url: str = "https://us-south.ml.cloud.ibm.com"
    watsonx_model_id: str = "ibm/granite-4-h-small"
    watsonx_api_version: str = "2024-03-14"
    ibm_iam_url: str = "https://iam.cloud.ibm.com/identity/token"

    # --- Profile conversation (FR-4) ---------------------------------------
    max_follow_ups: int = Field(default=3, ge=0)

    # --- Demo and single-service deploys --------------------------------------
    # DEMO_MODE: the /demo browser chat simulator replaces Twilio, and STT/LLM fall
    # back to offline demo engines when their keys are not set. Never in production.
    demo_mode: bool = False
    # Run background tasks inside the web process (no separate worker). For a free
    # single-instance deploy only; replies then take as long as the work itself.
    celery_eager: bool = False

    @field_validator("database_url")
    @classmethod
    def _psycopg_driver(cls, v: str) -> str:
        """Hosts like Render hand out postgres:// URLs; SQLAlchemy needs the driver."""
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix) :]
        return v

    @model_validator(mode="after")
    def _default_privacy_url(self) -> "Settings":
        if not self.privacy_notice_url:
            self.privacy_notice_url = self.public_base_url.rstrip("/") + "/privacy"
        return self

    @property
    def ai_demo_fallback(self) -> bool:
        """Use offline demo STT/LLM for whichever provider has no credentials."""
        return self.demo_mode

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "Settings":
        total = self.match_weight_skill + self.match_weight_distance + self.match_weight_experience
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"match weights must sum to 1.0, got {total:.4f}")
        return self

    @model_validator(mode="after")
    def _production_is_safe(self) -> "Settings":
        """Refuse to boot production (or staging) with example secrets or insecure URLs."""
        env = self.app_env
        if env not in ("production", "staging"):
            return self
        prod = env == "production"
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
        https_fields = ["public_base_url", "privacy_notice_url"]
        if prod:
            https_fields.insert(0, "dashboard_origin")
        for name in https_fields:
            if not getattr(self, name).startswith("https://"):
                problems.append(f"{name} must use https:// in {env}")
        if len(self.phone_hash_salt) < 32:
            problems.append(f"phone_hash_salt must be at least 32 characters in {env}")
        if prod and self.demo_mode:
            problems.append("demo_mode is not allowed in production")
        if prod and self.celery_eager:
            problems.append("celery_eager is not allowed in production")
        # In demo mode the simulator replaces Twilio and demo engines replace STT/LLM.
        if not self.demo_mode:
            for name in ("twilio_account_sid", "twilio_auth_token", "twilio_whatsapp_from"):
                if not getattr(self, name):
                    problems.append(f"{name} must be set in {env}")
            if not (self.watson_stt_api_key and self.watson_stt_url):
                problems.append(f"watson_stt_api_key and watson_stt_url must be set in {env}")
            llm_keys = {
                "openai": ("openai_api_key",),
                "watsonx": ("watsonx_api_key", "watsonx_project_id"),
            }[self.llm_provider]
            for name in llm_keys:
                if not getattr(self, name):
                    problems.append(f"{name} must be set in {env}")
        if prod and self.phone_encryption == "none":
            problems.append("phone numbers are stored unencrypted until Phase 7 adds encryption")
        if problems:
            raise ValueError(f"unsafe {env} settings: " + "; ".join(problems))
        return self

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
