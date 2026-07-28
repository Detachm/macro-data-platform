from __future__ import annotations

from datetime import date, time
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    provider_mode: Literal["fixture", "live"] = "fixture"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://macro:macro@127.0.0.1:5432/macro_data"
    service_token: SecretStr = SecretStr("development-only-token")
    provider_cursor_secret: SecretStr = SecretStr("development-only-provider-cursor-secret")
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, ge=1, le=65535)
    provider_timeout_seconds: int = Field(default=30, ge=1, le=300)
    hk_xtquant_host: str = "127.0.0.1"
    hk_xtquant_port: int = Field(default=58615, ge=1, le=65535)
    hk_xtquant_symbols: str = (
        "00700.HK,09988.HK,03690.HK,01810.HK,00941.HK,00005.HK,00388.HK,01299.HK,02318.HK,09618.HK"
    )
    us_provider_mode: Literal["fixture", "live"] = "fixture"
    twelve_data_api_key: SecretStr | None = None
    twelve_data_cursor_secret: SecretStr | None = None
    worker_schedule_timezone: str = "Asia/Shanghai"
    worker_schedule_hour_local: int = Field(default=7, ge=0, le=23)
    worker_schedule_minute_local: int = Field(default=50, ge=0, le=59)
    worker_schedule_poll_seconds: int = Field(default=60, ge=1, le=3600)
    worker_report_cutoff_hour_local: int = Field(default=8, ge=0, le=23)
    worker_report_cutoff_minute_local: int = Field(default=15, ge=0, le=59)
    worker_bar_lookback_days: int = Field(default=14, ge=7, le=366)
    worker_max_task_attempts: int = Field(default=3, ge=1, le=10)
    worker_max_report_attempts: int = Field(default=3, ge=1, le=10)
    worker_retry_delay_seconds: float = Field(default=1.0, gt=0, le=300)
    worker_market_freshness_hours: int = Field(default=36, ge=1, le=720)
    worker_news_freshness_hours: int = Field(default=24, ge=1, le=720)
    worker_run_once_report_date: date | None = None
    worker_backfill_start_date: date | None = None
    worker_backfill_end_date: date | None = None

    @model_validator(mode="after")
    def reject_development_secret_in_production(self) -> Settings:
        try:
            ZoneInfo(self.worker_schedule_timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("WORKER_SCHEDULE_TIMEZONE must be an IANA timezone") from error
        if time(self.worker_schedule_hour_local, self.worker_schedule_minute_local) >= time(
            self.worker_report_cutoff_hour_local,
            self.worker_report_cutoff_minute_local,
        ):
            raise ValueError("WORKER_SCHEDULE time must be before WORKER_REPORT_CUTOFF")
        if (
            self.app_env == "production"
            and self.service_token.get_secret_value() == "development-only-token"
        ):
            raise ValueError("SERVICE_TOKEN must be configured in production")
        if self.app_env == "production" and self.provider_mode != "live":
            raise ValueError("PROVIDER_MODE=live is required in production")
        if (
            self.app_env == "production"
            and self.provider_cursor_secret.get_secret_value()
            == "development-only-provider-cursor-secret"
        ):
            raise ValueError("PROVIDER_CURSOR_SECRET must be configured in production")
        if (self.worker_backfill_start_date is None) != (self.worker_backfill_end_date is None):
            raise ValueError(
                "WORKER_BACKFILL_START_DATE and WORKER_BACKFILL_END_DATE must be set together"
            )
        if (
            self.worker_backfill_start_date is not None
            and self.worker_backfill_end_date is not None
            and self.worker_backfill_end_date < self.worker_backfill_start_date
        ):
            raise ValueError(
                "WORKER_BACKFILL_END_DATE must not be before WORKER_BACKFILL_START_DATE"
            )
        if (
            self.worker_run_once_report_date is not None
            and self.worker_backfill_start_date is not None
        ):
            raise ValueError("WORKER_RUN_ONCE_REPORT_DATE cannot be combined with worker backfill")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
