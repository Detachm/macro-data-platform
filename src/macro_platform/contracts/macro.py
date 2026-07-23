from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from macro_platform.contracts.common import (
    AvailabilityBasis,
    DecimalValue,
    Region,
    SourceRef,
    StrictModel,
)


class Frequency(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    IRREGULAR = "irregular"


class RevisionPolicy(StrEnum):
    LATEST_AS_OF = "latest_as_of"
    FIRST_RELEASE = "first_release"
    ALL_VINTAGES = "all_vintages"


class MacroSeries(StrictModel):
    series_id: str
    region: Region
    authority: str
    code: str
    name: str
    description: str | None = None
    frequency: Frequency
    unit: str
    transformation: Literal["level", "mom", "qoq", "yoy", "annualized", "index"]
    seasonal_adjustment: Literal["adjusted", "not_adjusted", "unknown"]
    source: SourceRef


class MacroObservation(StrictModel):
    observation_id: str
    series_id: str
    region: Region
    period_start: date
    period_end: date
    value: DecimalValue | None
    unit: str
    transformation: str
    released_at: AwareDatetime | None
    available_at: AwareDatetime
    availability_basis: AvailabilityBasis
    vintage_id: str
    revision_no: int = Field(ge=0)
    value_status: Literal["estimate", "preliminary", "final"]
    supersedes_observation_id: str | None = None
    source: SourceRef
    quality_flags: list[str] = Field(default_factory=list)


class MacroRelease(StrictModel):
    release_id: str
    series_id: str
    region: Region
    release_name: str
    scheduled_at: AwareDatetime
    released_at: AwareDatetime | None = None
    available_at: AwareDatetime
    period_start: date
    period_end: date
    actual: DecimalValue | None = None
    consensus: DecimalValue | None = None
    previous: DecimalValue | None = None
    unit: str
    status: Literal["scheduled", "released", "delayed", "cancelled"]
    source: SourceRef


class MacroSeriesQuery(StrictModel):
    regions: set[Region] = Field(min_length=1)
    series_ids: list[str] = Field(default_factory=list, max_length=100)
    cursor: str | None = None
    limit: int = Field(default=500, ge=1, le=1000)


class MacroObservationQuery(StrictModel):
    series_ids: list[str] = Field(min_length=1, max_length=100)
    period_from: date
    period_to: date
    as_of: AwareDatetime
    revision_policy: RevisionPolicy = RevisionPolicy.LATEST_AS_OF
    cursor: str | None = None
    limit: int = Field(default=1000, ge=1, le=5000)

    @model_validator(mode="after")
    def validate_range(self) -> MacroObservationQuery:
        if self.period_from > self.period_to:
            raise ValueError("period_from must not be later than period_to")
        return self


class MacroReleaseQuery(StrictModel):
    regions: set[Region] = Field(min_length=1)
    scheduled_from: AwareDatetime
    scheduled_to: AwareDatetime
    as_of: AwareDatetime
    cursor: str | None = None
    limit: int = Field(default=500, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_range(self) -> MacroReleaseQuery:
        if self.scheduled_from >= self.scheduled_to:
            raise ValueError("scheduled_from must be earlier than scheduled_to")
        return self
