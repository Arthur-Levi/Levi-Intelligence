from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_name: str = "Levi Intelligence API"
    api_v1_prefix: str = "/api/v1"
    secret_key: str = Field(default="unsafe-local-development-key-change-me", min_length=32)
    access_token_expire_minutes: int = Field(default=30, ge=5, le=1440)
    database_url: str = "postgresql+asyncpg://levi:levi_local_only@localhost:5432/levi_intelligence"
    redis_url: str = "redis://localhost:6379/0"
    apify_api_token: str | None = None
    apify_actor_id: str | None = None
    worker_poll_seconds: float = Field(default=2.0, ge=0.2, le=60.0)
    discovery_max_attempts: int = Field(default=3, ge=1, le=10)
    cors_origins: str = "http://localhost:5173"
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        if self.app_env == "production" and self.secret_key.startswith("unsafe-"):
            raise ValueError("SECRET_KEY must be replaced before production")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
