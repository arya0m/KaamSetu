"""Application settings, loaded only from environment variables (or a local .env)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    readiness_timeout_s: float = Field(default=2.0, gt=0)

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "Settings":
        total = self.match_weight_skill + self.match_weight_distance + self.match_weight_experience
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"match weights must sum to 1.0, got {total:.4f}")
        return self

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
