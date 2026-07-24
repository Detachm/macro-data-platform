from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from macro_platform.contracts.common import (
    AvailabilityBasis,
    DecimalValue,
    Probability,
    Region,
    SourceRef,
    StrictModel,
    UsageRights,
)


class SourceTier(StrEnum):
    OFFICIAL = "official"
    LICENSED_MEDIA = "licensed_media"
    RESEARCH = "research"
    SOCIAL = "social"
    OTHER = "other"


class ContentMode(StrEnum):
    HEADLINE = "headline"
    SNIPPET = "snippet"
    FULL_TEXT = "full_text"


class EntityRef(StrictModel):
    entity_type: Literal[
        "instrument",
        "company",
        "country",
        "sector",
        "person",
        "organization",
        "commodity",
        "currency",
    ]
    entity_id: str
    mention: str | None = None
    confidence: Probability


class VendorAnnotation(StrictModel):
    provider_id: str
    annotation_type: Literal["sentiment", "importance", "event", "attention"]
    label: str | None = None
    score: DecimalValue | None = None
    scale_min: DecimalValue | None = None
    scale_max: DecimalValue | None = None
    model_version: str | None = None


class NewsEvent(StrictModel):
    news_id: str
    cluster_id: str | None = None
    supersedes_news_id: str | None = None
    status: Literal["active", "corrected", "retracted"] = "active"
    title: Annotated[str, Field(min_length=1, max_length=1000)]
    summary: Annotated[str, Field(max_length=10000)] | None = None
    body: str | None = None
    content_mode: ContentMode
    language: Annotated[str, Field(pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$")]
    source_name: str
    source_tier: SourceTier
    canonical_url: HttpUrl | None = None
    published_at: AwareDatetime
    first_seen_at: AwareDatetime
    available_at: AwareDatetime
    availability_basis: AvailabilityBasis
    regions: list[Region] = Field(min_length=1)
    entities: list[EntityRef] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    vendor_annotations: list[VendorAnnotation] = Field(default_factory=list)
    content_hash_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    usage_rights: UsageRights
    source: SourceRef
    quality_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content_rights(self) -> NewsEvent:
        if self.body is not None and self.content_mode is not ContentMode.FULL_TEXT:
            raise ValueError("body requires content_mode=full_text")
        if self.body is not None and not self.usage_rights.storage_allowed:
            raise ValueError("body cannot be retained when storage is not allowed")
        if self.available_at < self.published_at:
            raise ValueError("available_at cannot precede published_at")
        return self


class NewsQuery(StrictModel):
    regions: set[Region] = Field(min_length=1)
    published_from: AwareDatetime
    published_to: AwareDatetime
    as_of: AwareDatetime
    entity_ids: list[str] = Field(default_factory=list, max_length=100)
    topics: list[str] = Field(default_factory=list, max_length=50)
    languages: set[str] = Field(default_factory=set)
    source_tiers: set[SourceTier] = Field(default_factory=set)
    include_superseded: bool = False
    content_mode: ContentMode = ContentMode.SNIPPET
    cursor: str | None = None
    limit: int = Field(default=100, ge=1, le=500)

    @model_validator(mode="after")
    def validate_range(self) -> NewsQuery:
        if self.published_from >= self.published_to:
            raise ValueError("published_from must be earlier than published_to")
        return self
