from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import TypeVar

from macro_platform.contracts.common import Region
from macro_platform.contracts.editor import (
    CoverageItem,
    EditorContext,
    EditorContextRequest,
    ResolvedContextSelection,
)
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
)
from macro_platform.contracts.news import ContentMode, NewsEvent, NewsQuery
from macro_platform.contracts.provider import Dataset
from macro_platform.governance.source_policy import (
    NonProductionSourcePolicy,
    PolicyPurpose,
    SourcePolicy,
)
from macro_platform.normalization.common import canonical_json_checksum, stable_id, utc_now
from macro_platform.services.macro_service import MacroService
from macro_platform.services.market_service import MarketService
from macro_platform.services.news_service import NewsService

RecordT = TypeVar("RecordT")


class DataUnavailableError(RuntimeError):
    pass


class EditorContextService:
    def __init__(
        self,
        market_service: MarketService,
        macro_service: MacroService,
        news_service: NewsService,
        *,
        source_policy: SourcePolicy | None = None,
    ) -> None:
        self._market = market_service
        self._macro = macro_service
        self._news = news_service
        self._source_policy = source_policy or NonProductionSourcePolicy()

    async def build(self, request: EditorContextRequest) -> EditorContext:
        as_of = request.as_of or utc_now()

        snapshots = []
        bars = []
        if request.market.instrument_ids:
            snapshots = await self._market.snapshots(
                MarketSnapshotQuery(instrument_ids=request.market.instrument_ids, as_of=as_of)
            )
            bars = await self._market.bars(
                BarQuery(
                    instrument_ids=request.market.instrument_ids,
                    interval=Interval.D1,
                    start=as_of - timedelta(days=request.market.lookback_sessions * 3),
                    end=as_of + timedelta(microseconds=1),
                    adjustment=Adjustment.RAW,
                    as_of=as_of,
                )
            )
            snapshots = self._filter_snapshots(snapshots)
            bars = self._filter_records(
                bars,
                dataset=Dataset.BARS,
                source_and_region=lambda record: (record.source.provider_id, record.region),
            )

        market_observations = []
        if request.market.metric_codes:
            market_observations = await self._market.observations(
                MarketObservationQuery(
                    regions=request.regions,
                    metric_codes=request.market.metric_codes,
                    start=as_of - timedelta(days=request.market.lookback_sessions * 3),
                    end=as_of + timedelta(microseconds=1),
                    as_of=as_of,
                )
            )
            market_observations = self._filter_records(
                market_observations,
                dataset=Dataset.MARKET_OBSERVATIONS,
                source_and_region=lambda record: (record.source.provider_id, record.region),
            )

        macro_observations = []
        if request.macro.series_ids:
            period_from = (as_of - timedelta(days=request.macro.lookback_days)).date()
            macro_observations = await self._macro.observations(
                MacroObservationQuery(
                    series_ids=request.macro.series_ids,
                    period_from=period_from,
                    period_to=as_of.date(),
                    as_of=as_of,
                    revision_policy=request.macro.revision_policy,
                )
            )
            macro_observations = self._filter_records(
                macro_observations,
                dataset=Dataset.MACRO_OBSERVATIONS,
                source_and_region=lambda record: (record.source.provider_id, record.region),
            )

        macro_releases = await self._macro.releases(
            MacroReleaseQuery(
                regions=request.regions,
                scheduled_from=as_of - timedelta(days=request.macro.lookback_days),
                scheduled_to=as_of + timedelta(days=request.macro.upcoming_days),
                as_of=as_of,
            )
        )
        macro_releases = self._filter_records(
            macro_releases,
            dataset=Dataset.MACRO_RELEASES,
            source_and_region=lambda record: (record.source.provider_id, record.region),
        )

        requested_news_mode = ContentMode(request.news.content_mode)
        news_events = await self._news.events(
            NewsQuery(
                regions=request.regions,
                published_from=as_of - timedelta(hours=request.news.lookback_hours),
                published_to=as_of + timedelta(microseconds=1),
                as_of=as_of,
                topics=request.news.topics,
                languages=request.news.languages,
                source_tiers=request.news.source_tiers,
                content_mode=requested_news_mode,
                limit=request.news.max_items,
            ),
            for_external_llm=True,
        )
        news_events = self._filter_news_events(news_events)
        news_events = self._limit_news_clusters(news_events, request.news.max_per_cluster)

        coverage = self._coverage(
            regions=request.regions,
            market_records=[*snapshots, *bars, *market_observations],
            macro_records=[*macro_observations, *macro_releases],
            news_records=news_events,
        )
        if request.fail_on_incomplete and any(item.status == "unavailable" for item in coverage):
            raise DataUnavailableError("one or more required datasets are unavailable")

        selection = ResolvedContextSelection(
            preset_id=request.preset_id,
            preset_version="1.0.0",
            instrument_ids=request.market.instrument_ids,
            series_ids=request.macro.series_ids,
            metric_codes=request.market.metric_codes,
            topic_taxonomy_version="1.0.0",
        )
        fingerprint_payload = {
            "as_of": as_of.isoformat(),
            "selection": selection.model_dump(mode="json"),
            "market_snapshots": [item.model_dump(mode="json") for item in snapshots],
            "market_bars": [item.model_dump(mode="json") for item in bars],
            "market_observations": [item.model_dump(mode="json") for item in market_observations],
            "macro_observations": [item.model_dump(mode="json") for item in macro_observations],
            "macro_releases": [item.model_dump(mode="json") for item in macro_releases],
            "news_events": [item.model_dump(mode="json") for item in news_events],
            "coverage": [item.model_dump(mode="json") for item in coverage],
        }
        fingerprint = canonical_json_checksum(fingerprint_payload)
        generated_at = utc_now()
        return EditorContext(
            context_id=stable_id("ctx", as_of.isoformat(), fingerprint),
            generated_at=generated_at,
            as_of=as_of,
            resolved_selection=selection,
            market_snapshots=snapshots,
            market_bars=bars,
            market_observations=market_observations,
            macro_observations=macro_observations,
            macro_releases=macro_releases,
            news_events=news_events,
            coverage=coverage,
            data_fingerprint_sha256=fingerprint,
        )

    def _filter_snapshots(self, records: list[MarketSnapshot]) -> list[MarketSnapshot]:
        return [
            record
            for record in records
            if all(
                self._source_is_allowed_for_llm_context(
                    source.provider_id,
                    Dataset.BARS,
                    record.region,
                )
                for source in record.source_records
            )
        ]

    def _filter_records(
        self,
        records: list[RecordT],
        *,
        dataset: Dataset,
        source_and_region: Callable[[RecordT], tuple[str, Region]],
    ) -> list[RecordT]:
        selected: list[RecordT] = []
        for record in records:
            provider_id, region = source_and_region(record)
            if self._source_is_allowed_for_llm_context(provider_id, dataset, region):
                selected.append(record)
        return selected

    def _filter_news_events(self, records: list[NewsEvent]) -> list[NewsEvent]:
        return [
            record
            for record in records
            if all(
                self._source_is_allowed_for_llm_context(
                    record.source.provider_id,
                    Dataset.NEWS,
                    region,
                )
                for region in record.regions
            )
        ]

    def _source_is_allowed_for_llm_context(
        self,
        provider_id: str,
        dataset: Dataset,
        region: Region,
    ) -> bool:
        return all(
            self._source_policy.decision(
                provider_id=provider_id,
                dataset=dataset,
                region=region,
                purpose=purpose,
            ).allowed
            for purpose in {PolicyPurpose.EDITOR_CONTEXT, PolicyPurpose.EXTERNAL_LLM}
        )

    @staticmethod
    def _limit_news_clusters(events: list[NewsEvent], max_per_cluster: int) -> list[NewsEvent]:
        counts: dict[str, int] = {}
        selected: list[NewsEvent] = []
        for event in events:
            key = event.cluster_id or event.news_id
            current = counts.get(key, 0)
            if current >= max_per_cluster:
                continue
            counts[key] = current + 1
            selected.append(event)
        return selected

    @staticmethod
    def _coverage(
        *,
        regions: set[Region],
        market_records: list[MarketSnapshot | MarketBar | MarketObservation],
        macro_records: list[MacroObservation | MacroRelease],
        news_records: list[NewsEvent],
    ) -> list[CoverageItem]:
        result: list[CoverageItem] = []
        for region in sorted(regions, key=str):
            regional_market = [item for item in market_records if item.region is region]
            regional_macro = [item for item in macro_records if item.region is region]
            regional_news = [item for item in news_records if region in item.regions]
            for dataset, records in (
                ("market", regional_market),
                ("macro", regional_macro),
                ("news", regional_news),
            ):
                providers: set[str] = set()
                available_at = []
                for record in records:
                    available_at.append(record.available_at)
                    if isinstance(record, MarketSnapshot):
                        providers.update(source.provider_id for source in record.source_records)
                    else:
                        providers.add(record.source.provider_id)
                count = len(records)
                result.append(
                    CoverageItem(
                        dataset=dataset,
                        region=region,
                        status="complete" if count else "unavailable",
                        record_count=count,
                        newest_available_at=max(available_at) if available_at else None,
                        providers=sorted(providers),
                        reasons=[] if count else ["no records available in the current repository"],
                    )
                )
        return result
