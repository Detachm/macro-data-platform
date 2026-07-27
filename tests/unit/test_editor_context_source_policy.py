from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from macro_platform.contracts.common import AvailabilityBasis, Region
from macro_platform.contracts.editor import EditorContextRequest
from macro_platform.contracts.macro import (
    MacroObservation,
    MacroObservationQuery,
    MacroRelease,
    MacroReleaseQuery,
)
from macro_platform.contracts.market import (
    Adjustment,
    BarQuery,
    Interval,
    MarketBar,
    MarketObservation,
    MarketObservationQuery,
    MarketSnapshot,
    MarketSnapshotQuery,
    ScopeType,
)
from macro_platform.contracts.news import NewsEvent, NewsQuery
from macro_platform.contracts.provider import Dataset
from macro_platform.governance.source_policy import (
    ApprovalStatus,
    ProductionSourcePolicy,
    RetentionRule,
    SourcePolicyEntry,
    SourcePolicyManifest,
    load_production_source_policy,
)
from macro_platform.services.editor_context_service import EditorContextService
from macro_platform.services.macro_service import MacroService
from macro_platform.services.market_service import MarketService
from macro_platform.services.news_service import NewsService
from macro_platform.storage.repositories import EmptyDataRepository
from tests.helpers import NOW, news_event, source_ref


class _RecordsRepository(EmptyDataRepository):
    def __init__(self, provider_id: str = "unresolved.provider.v1") -> None:
        source = source_ref(provider_id)
        self.snapshot = MarketSnapshot(
            instrument_id="ins_unresolved",
            canonical_symbol="XNAS:UNRESOLVED",
            region=Region.US,
            price_time=NOW,
            last=Decimal("1"),
            currency="USD",
            available_at=NOW,
            source_records=[source],
        )
        self.bar = MarketBar(
            bar_id="bar_unresolved",
            instrument_id="ins_unresolved",
            canonical_symbol="XNAS:UNRESOLVED",
            region=Region.US,
            interval=Interval.D1,
            bar_start=NOW - timedelta(days=1),
            bar_end=NOW,
            trading_date=(NOW - timedelta(days=1)).date(),
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
            currency="USD",
            adjustment=Adjustment.RAW,
            available_at=NOW,
            availability_basis=AvailabilityBasis.FIRST_SEEN,
            source=source,
        )
        self.market_observation = MarketObservation(
            observation_id="market_unresolved",
            region=Region.US,
            scope_type=ScopeType.MARKET,
            scope_id="US",
            metric_code="market.turnover",
            value=Decimal("1"),
            unit="USD",
            period_start=NOW - timedelta(days=1),
            period_end=NOW,
            observed_at=NOW,
            available_at=NOW,
            availability_basis=AvailabilityBasis.FIRST_SEEN,
            source=source,
        )
        self.macro_observation = MacroObservation(
            observation_id="macro_unresolved",
            series_id="macro:US:TEST:UNRESOLVED",
            region=Region.US,
            period_start=NOW.date(),
            period_end=NOW.date(),
            value=Decimal("1"),
            unit="index",
            transformation="level",
            released_at=NOW,
            available_at=NOW,
            availability_basis=AvailabilityBasis.FIRST_SEEN,
            vintage_id="v1",
            revision_no=0,
            value_status="preliminary",
            source=source,
        )
        self.macro_release = MacroRelease(
            release_id="release_unresolved",
            series_id="macro:US:TEST:UNRESOLVED",
            region=Region.US,
            release_name="Unresolved release",
            scheduled_at=NOW,
            released_at=NOW,
            available_at=NOW,
            period_start=NOW.date(),
            period_end=NOW.date(),
            unit="index",
            status="released",
            source=source,
        )
        self.news = news_event().model_copy(update={"news_id": "news_unresolved", "source": source})

    async def list_snapshots(self, query: MarketSnapshotQuery) -> list[MarketSnapshot]:
        return [self.snapshot]

    async def list_bars(self, query: BarQuery) -> list[MarketBar]:
        return [self.bar]

    async def list_market_observations(
        self, query: MarketObservationQuery
    ) -> list[MarketObservation]:
        return [self.market_observation]

    async def list_macro_observations(self, query: MacroObservationQuery) -> list[MacroObservation]:
        return [self.macro_observation]

    async def list_macro_releases(self, query: MacroReleaseQuery) -> list[MacroRelease]:
        return [self.macro_release]

    async def list_news(self, query: NewsQuery) -> list[NewsEvent]:
        return [self.news]


def _production_policy() -> ProductionSourcePolicy:
    return ProductionSourcePolicy(
        SourcePolicyManifest(
            policy_version="test",
            entries=[
                SourcePolicyEntry(
                    policy_id="unrelated-approved-entry",
                    provider_id="approved.provider.v1",
                    dataset=Dataset.NEWS,
                    regions={Region.US},
                    owner="@kazming666",
                    credential_requirement="none",
                    ingestion_allowed=True,
                    external_llm_allowed=True,
                    citation_allowed=True,
                    retention_rule=RetentionRule.METADATA_ONLY,
                    approval_status=ApprovalStatus.APPROVED,
                    production_enabled=True,
                    evidence=["docs/data-sources/us-mvp.md"],
                )
            ],
        )
    )


async def test_gov_026_editor_context_rejects_unresolved_sources_from_every_dataset() -> None:
    repository = _RecordsRepository()
    policy = _production_policy()
    service = EditorContextService(
        MarketService(repository),
        MacroService(repository),
        NewsService(repository, source_policy=policy),
        source_policy=policy,
    )

    context = await service.build(
        EditorContextRequest(
            regions={Region.US},
            as_of=NOW,
            market={
                "instrument_ids": ["ins_unresolved"],
                "metric_codes": ["market.turnover"],
            },
            macro={"series_ids": ["macro:US:TEST:UNRESOLVED"]},
        )
    )

    assert context.market_snapshots == []
    assert context.market_bars == []
    assert context.market_observations == []
    assert context.macro_observations == []
    assert context.macro_releases == []
    assert context.news_events == []


async def test_gov_026_editor_context_rejects_snapshots_without_source_records() -> None:
    repository = _RecordsRepository()
    repository.snapshot = repository.snapshot.model_copy(update={"source_records": []})
    policy = _production_policy()
    service = EditorContextService(
        MarketService(repository),
        MacroService(repository),
        NewsService(repository, source_policy=policy),
        source_policy=policy,
    )

    context = await service.build(
        EditorContextRequest(
            regions={Region.US},
            as_of=NOW,
            market={"instrument_ids": ["ins_unresolved"], "metric_codes": ["market.turnover"]},
            macro={"series_ids": ["macro:US:TEST:UNRESOLVED"]},
        )
    )

    assert context.market_snapshots == []


def _external_llm_denied_policy() -> ProductionSourcePolicy:
    provider_id = "llm-denied.provider.v1"
    datasets = (
        Dataset.BARS,
        Dataset.MARKET_OBSERVATIONS,
        Dataset.MACRO_OBSERVATIONS,
        Dataset.MACRO_RELEASES,
        Dataset.NEWS,
    )
    return ProductionSourcePolicy(
        SourcePolicyManifest(
            policy_version="test",
            entries=[
                SourcePolicyEntry(
                    policy_id=f"llm-denied-{dataset.value}",
                    provider_id=provider_id,
                    dataset=dataset,
                    regions={Region.US},
                    owner="@kazming666",
                    credential_requirement="none",
                    ingestion_allowed=True,
                    external_llm_allowed=False,
                    citation_allowed=True,
                    retention_rule=RetentionRule.CANONICAL_FACTS,
                    approval_status=ApprovalStatus.APPROVED,
                    production_enabled=True,
                    evidence=["docs/data-sources/us-mvp.md"],
                )
                for dataset in datasets
            ],
        )
    )


async def test_gov_026_editor_context_excludes_sources_denied_for_external_llm() -> None:
    policy = _external_llm_denied_policy()
    repository = _RecordsRepository("llm-denied.provider.v1")
    service = EditorContextService(
        MarketService(repository),
        MacroService(repository),
        NewsService(repository, source_policy=policy),
        source_policy=policy,
    )

    context = await service.build(
        EditorContextRequest(
            regions={Region.US},
            as_of=NOW,
            market={
                "instrument_ids": ["ins_unresolved"],
                "metric_codes": ["market.turnover"],
            },
            macro={"series_ids": ["macro:US:TEST:UNRESOLVED"]},
        )
    )

    assert context.market_snapshots == []
    assert context.market_bars == []
    assert context.market_observations == []
    assert context.macro_observations == []
    assert context.macro_releases == []
    assert context.news_events == []


def _citation_denied_policy() -> ProductionSourcePolicy:
    provider_id = "citation-denied.provider.v1"
    datasets = (
        Dataset.BARS,
        Dataset.MARKET_OBSERVATIONS,
        Dataset.MACRO_OBSERVATIONS,
        Dataset.MACRO_RELEASES,
        Dataset.NEWS,
    )
    return ProductionSourcePolicy(
        SourcePolicyManifest(
            policy_version="test",
            entries=[
                SourcePolicyEntry(
                    policy_id=f"citation-denied-{dataset.value}",
                    provider_id=provider_id,
                    dataset=dataset,
                    regions={Region.US},
                    owner="@kazming666",
                    credential_requirement="none",
                    ingestion_allowed=True,
                    external_llm_allowed=True,
                    citation_allowed=False,
                    retention_rule=RetentionRule.CANONICAL_FACTS,
                    approval_status=ApprovalStatus.APPROVED,
                    production_enabled=True,
                    evidence=["docs/data-sources/us-mvp.md"],
                )
                for dataset in datasets
            ],
        )
    )


async def test_gov_026_editor_context_excludes_sources_denied_for_citation() -> None:
    policy = _citation_denied_policy()
    repository = _RecordsRepository("citation-denied.provider.v1")
    service = EditorContextService(
        MarketService(repository),
        MacroService(repository),
        NewsService(repository, source_policy=policy),
        source_policy=policy,
    )

    context = await service.build(
        EditorContextRequest(
            regions={Region.US},
            as_of=NOW,
            market={
                "instrument_ids": ["ins_unresolved"],
                "metric_codes": ["market.turnover"],
            },
            macro={"series_ids": ["macro:US:TEST:UNRESOLVED"]},
        )
    )

    assert context.market_snapshots == []
    assert context.market_bars == []
    assert context.market_observations == []
    assert context.macro_observations == []
    assert context.macro_releases == []
    assert context.news_events == []


async def test_gov_026_twelve_data_bars_are_excluded_from_production_editor_context() -> None:
    policy = load_production_source_policy()
    repository = _RecordsRepository("us.twelve-data.v1")
    service = EditorContextService(
        MarketService(repository),
        MacroService(repository),
        NewsService(repository, source_policy=policy),
        source_policy=policy,
    )

    context = await service.build(
        EditorContextRequest(
            regions={Region.US},
            as_of=NOW,
            market={"instrument_ids": ["ins_unresolved"]},
        )
    )

    assert context.market_snapshots == []
    assert context.market_bars == []
