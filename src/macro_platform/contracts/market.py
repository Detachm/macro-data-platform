from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, Field, model_validator

from macro_platform.contracts.common import (
    AssetClass,
    AvailabilityBasis,
    DecimalValue,
    Region,
    SourceRef,
    StrictModel,
)


class InstrumentStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELISTED = "delisted"
    UNKNOWN = "unknown"


class Interval(StrEnum):
    D1 = "1d"
    W1 = "1w"
    MO1 = "1mo"


class Adjustment(StrEnum):
    RAW = "raw"
    SPLIT_ADJUSTED = "split_adjusted"
    TOTAL_RETURN = "total_return"


class ScopeType(StrEnum):
    INSTRUMENT = "instrument"
    MARKET = "market"
    EXCHANGE = "exchange"
    SECTOR = "sector"


class Instrument(StrictModel):
    instrument_id: Annotated[str, Field(min_length=6, max_length=64)]
    canonical_symbol: Annotated[str, Field(pattern=r"^[A-Z0-9]{4,12}:.+$")]
    region: Region
    venue_mic: Annotated[str, Field(pattern=r"^[A-Z0-9]{4,12}$")]
    local_symbol: Annotated[str, Field(min_length=1, max_length=32)]
    name: Annotated[str, Field(min_length=1, max_length=256)]
    name_en: str | None = None
    asset_class: AssetClass
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    timezone: Annotated[str, Field(min_length=3, max_length=64)]
    status: InstrumentStatus
    listed_on: date | None = None
    delisted_on: date | None = None
    lot_size: DecimalValue | None = None
    valid_from: date
    valid_to: date | None = None
    source: SourceRef


class MarketBar(StrictModel):
    bar_id: str
    instrument_id: str
    canonical_symbol: str
    region: Region
    interval: Interval
    bar_start: AwareDatetime
    bar_end: AwareDatetime
    trading_date: date
    open: DecimalValue
    high: DecimalValue
    low: DecimalValue
    close: DecimalValue
    volume: DecimalValue | None = Field(default=None, ge=0)
    turnover: DecimalValue | None = Field(default=None, ge=0)
    vwap: DecimalValue | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    adjustment: Adjustment
    adjustment_as_of: AwareDatetime | None = None
    available_at: AwareDatetime
    availability_basis: AvailabilityBasis
    source: SourceRef
    quality_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_bar(self) -> MarketBar:
        if self.bar_start >= self.bar_end:
            raise ValueError("bar_start must be earlier than bar_end")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("open and close must be within low/high")
        if self.low > self.high:
            raise ValueError("low must not exceed high")
        if self.adjustment is not Adjustment.RAW and self.adjustment_as_of is None:
            raise ValueError("adjustment_as_of is required for adjusted bars")
        return self


class MarketObservation(StrictModel):
    observation_id: str
    region: Region
    scope_type: ScopeType
    scope_id: str
    metric_code: str
    value: DecimalValue | None
    unit: str
    currency: str | None = None
    period_start: AwareDatetime
    period_end: AwareDatetime
    observed_at: AwareDatetime
    available_at: AwareDatetime
    availability_basis: AvailabilityBasis
    dimensions: dict[str, str] = Field(default_factory=dict)
    source: SourceRef
    quality_flags: list[str] = Field(default_factory=list)


class MarketSnapshot(StrictModel):
    instrument_id: str
    canonical_symbol: str
    region: Region
    price_time: AwareDatetime
    last: DecimalValue
    previous_close: DecimalValue | None = None
    change: DecimalValue | None = None
    change_pct: DecimalValue | None = None
    volume: DecimalValue | None = None
    turnover: DecimalValue | None = None
    currency: str
    available_at: AwareDatetime
    source_records: list[SourceRef]


class InstrumentQuery(StrictModel):
    regions: set[Region] = Field(min_length=1)
    venues: set[str] = Field(default_factory=set)
    asset_classes: set[AssetClass] = Field(default_factory=set)
    active_on: date | None = None
    modified_since: AwareDatetime | None = None
    cursor: str | None = None
    limit: int = Field(default=500, ge=1, le=1000)


class BarQuery(StrictModel):
    instrument_ids: list[str] = Field(min_length=1, max_length=100)
    interval: Interval
    start: AwareDatetime
    end: AwareDatetime
    adjustment: Adjustment = Adjustment.RAW
    as_of: AwareDatetime
    cursor: str | None = None
    limit: int = Field(default=1000, ge=1, le=5000)

    @model_validator(mode="after")
    def validate_range(self) -> BarQuery:
        if self.start >= self.end:
            raise ValueError("start must be earlier than end")
        return self


class MarketSnapshotQuery(StrictModel):
    instrument_ids: list[str] = Field(min_length=1, max_length=100)
    as_of: AwareDatetime


class MarketObservationQuery(StrictModel):
    regions: set[Region] = Field(min_length=1)
    metric_codes: list[str] = Field(min_length=1, max_length=50)
    scope_ids: list[str] = Field(default_factory=list, max_length=100)
    start: AwareDatetime
    end: AwareDatetime
    as_of: AwareDatetime
    cursor: str | None = None
    limit: int = Field(default=1000, ge=1, le=5000)

    @model_validator(mode="after")
    def validate_range(self) -> MarketObservationQuery:
        if self.start >= self.end:
            raise ValueError("start must be earlier than end")
        return self
