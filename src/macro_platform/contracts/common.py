from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl


class StrictModel(BaseModel):
    """Immutable public contract that rejects unknown fields."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        populate_by_name=True,
    )


DecimalValue = Annotated[Decimal, Field(max_digits=38, decimal_places=18)]
Probability = Annotated[DecimalValue, Field(ge=Decimal("0"), le=Decimal("1"))]


class Region(StrEnum):
    CN = "CN"
    HK = "HK"
    US = "US"
    GLOBAL = "GLOBAL"


class AssetClass(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    INDEX = "index"
    FUTURE = "future"
    FX = "fx"
    RATE = "rate"
    COMMODITY = "commodity"


class AvailabilityBasis(StrEnum):
    PROVIDER_DISSEMINATED = "provider_disseminated"
    FIRST_SEEN = "first_seen"
    EXCHANGE_PUBLISHED = "exchange_published"
    INFERRED = "inferred"


class SourceRef(StrictModel):
    provider_id: Annotated[str, Field(min_length=2, max_length=64)]
    provider_record_id: Annotated[str, Field(min_length=1, max_length=256)]
    source_name: Annotated[str, Field(min_length=1, max_length=256)]
    source_url: HttpUrl | None = None
    source_symbol: str | None = None
    retrieved_at: AwareDatetime
    provider_updated_at: AwareDatetime | None = None
    checksum_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class UsageRights(StrictModel):
    storage_allowed: bool
    internal_analysis_allowed: bool
    external_llm_allowed: bool
    embedding_allowed: bool
    redistribution_allowed: bool
    content_expires_at: AwareDatetime | None = None


class WarningItem(StrictModel):
    code: Annotated[str, Field(min_length=2, max_length=64)]
    message: Annotated[str, Field(min_length=1, max_length=500)]
    scope: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorDetail(StrictModel):
    location: list[str | int]
    message: str
    error_type: str


class ApiError(StrictModel):
    code: str
    message: str
    retryable: bool
    retry_after_seconds: int | None = Field(default=None, ge=0)
    details: list[ErrorDetail] = Field(default_factory=list)


class ErrorEnvelope(StrictModel):
    request_id: UUID
    api_version: Literal["v1"] = "v1"
    error: ApiError


class PageMeta(StrictModel):
    limit: int = Field(ge=1)
    has_more: bool
    next_cursor: str | None = None


class SuccessEnvelope[T](StrictModel):
    request_id: UUID
    api_version: Literal["v1"] = "v1"
    as_of: AwareDatetime
    snapshot_at: AwareDatetime
    data: T
    page: PageMeta | None = None
    warnings: list[WarningItem] = Field(default_factory=list)


class ItemList[T](StrictModel):
    items: list[T]
