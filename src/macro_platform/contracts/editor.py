from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field

from macro_platform.contracts.common import Region, StrictModel
from macro_platform.contracts.macro import MacroObservation, MacroRelease, RevisionPolicy
from macro_platform.contracts.market import MarketBar, MarketObservation, MarketSnapshot
from macro_platform.contracts.news import NewsEvent, SourceTier


class MarketContextSpec(StrictModel):
    instrument_ids: list[str] = Field(default_factory=list, max_length=100)
    lookback_sessions: int = Field(default=5, ge=2, le=30)
    metric_codes: list[str] = Field(default_factory=list, max_length=50)


class MacroContextSpec(StrictModel):
    series_ids: list[str] = Field(default_factory=list, max_length=100)
    lookback_days: int = Field(default=120, ge=1, le=3660)
    upcoming_days: int = Field(default=7, ge=0, le=90)
    revision_policy: RevisionPolicy = RevisionPolicy.LATEST_AS_OF


class NewsContextSpec(StrictModel):
    lookback_hours: int = Field(default=24, ge=1, le=744)
    topics: list[str] = Field(default_factory=list, max_length=50)
    source_tiers: set[SourceTier] = Field(default_factory=set)
    languages: set[str] = Field(default_factory=set)
    max_items: int = Field(default=100, ge=1, le=500)
    max_per_cluster: int = Field(default=1, ge=1, le=10)
    content_mode: Literal["headline", "snippet"] = "snippet"


class EditorContextRequest(StrictModel):
    as_of: AwareDatetime | None = None
    regions: set[Region] = Field(min_length=1)
    preset_id: str = "daily_macro_v1"
    market: MarketContextSpec = Field(default_factory=MarketContextSpec)
    macro: MacroContextSpec = Field(default_factory=MacroContextSpec)
    news: NewsContextSpec = Field(default_factory=NewsContextSpec)
    require_point_in_time: bool = True
    fail_on_incomplete: bool = False


class CoverageItem(StrictModel):
    dataset: str
    region: Region
    status: Literal["complete", "partial", "stale", "unavailable"]
    record_count: int = Field(ge=0)
    newest_available_at: AwareDatetime | None = None
    providers: list[str]
    reasons: list[str] = Field(default_factory=list)


class ResolvedContextSelection(StrictModel):
    preset_id: str
    preset_version: str
    instrument_ids: list[str]
    series_ids: list[str]
    metric_codes: list[str]
    topic_taxonomy_version: str


class EditorContext(StrictModel):
    context_id: str
    context_version: Literal["1.0"] = "1.0"
    generated_at: AwareDatetime
    as_of: AwareDatetime
    resolved_selection: ResolvedContextSelection
    market_snapshots: list[MarketSnapshot]
    market_bars: list[MarketBar]
    market_observations: list[MarketObservation]
    macro_observations: list[MacroObservation]
    macro_releases: list[MacroRelease]
    news_events: list[NewsEvent]
    coverage: list[CoverageItem]
    data_fingerprint_sha256: str
