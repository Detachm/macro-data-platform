from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://macro:macro@127.0.0.1:5432/macro_data"
    service_token: SecretStr = SecretStr("development-only-token")
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, ge=1, le=65535)
    provider_timeout_seconds: int = Field(default=30, ge=1, le=300)
    us_provider_mode: Literal["fixture", "live"] = "fixture"
    twelve_data_api_key: SecretStr | None = None
    twelve_data_cursor_secret: SecretStr | None = None

    @model_validator(mode="after")
    def reject_development_secret_in_production(self) -> Settings:
        if (
            self.app_env == "production"
            and self.service_token.get_secret_value() == "development-only-token"
        ):
            raise ValueError("SERVICE_TOKEN must be configured in production")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
