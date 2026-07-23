from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from macro_platform.contracts.common import Region, StrictModel, WarningItem
from macro_platform.contracts.market import Interval


class Dataset(StrEnum):
    INSTRUMENTS = "instruments"
    BARS = "bars"
    MARKET_OBSERVATIONS = "market_observations"
    MACRO_SERIES = "macro_series"
    MACRO_OBSERVATIONS = "macro_observations"
    MACRO_RELEASES = "macro_releases"
    NEWS = "news"


class FetchContext(StrictModel):
    request_id: UUID
    as_of: AwareDatetime
    deadline_at: AwareDatetime


class ProviderPage[T: StrictModel](StrictModel):
    items: list[T]
    next_cursor: str | None = None
    source_watermark: str | None = None
    fetched_at: AwareDatetime
    complete: bool
    warnings: list[WarningItem] = Field(default_factory=list)


class ProviderCapabilities(StrictModel):
    provider_id: str
    regions: set[Region]
    datasets: set[Dataset]
    intervals: set[Interval] = Field(default_factory=set)
    max_page_size: int = Field(ge=1, le=10000)
    supports_point_in_time: bool
    supports_revisions: bool
    supports_full_text: bool
    external_llm_allowed: bool


class ProviderHealth(StrictModel):
    provider_id: str
    status: Literal["ok", "degraded", "down", "not_configured"]
    checked_at: AwareDatetime
    latency_ms: int = Field(ge=0)
    message: str | None = None


class IngestJobRequest(StrictModel):
    provider_role: str
    dataset: Dataset
    regions: set[Region]
    start: AwareDatetime
    end: AwareDatetime
    as_of: AwareDatetime
    cursor: str | None = None
    dry_run: bool = False
    force: bool = False


class IngestJobResult(StrictModel):
    run_id: UUID
    status: Literal["succeeded", "partial", "failed", "retry_wait"]
    provider_role: str
    dataset: Dataset
    started_at: AwareDatetime
    finished_at: AwareDatetime
    records_fetched: int = Field(ge=0)
    records_accepted: int = Field(ge=0)
    records_rejected: int = Field(ge=0)
    records_inserted: int = Field(ge=0)
    records_updated: int = Field(ge=0)
    next_cursor: str | None = None
    source_watermark: str | None = None
    error_code: str | None = None
    retry_after_seconds: int | None = Field(default=None, ge=0)
    warnings: list[WarningItem] = Field(default_factory=list)
