"""Durable page ingestion for approved release-calendar and headline providers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from macro_platform.contracts.common import Region, WarningItem
from macro_platform.contracts.macro import Frequency, MacroRelease, MacroReleaseQuery, MacroSeries
from macro_platform.contracts.news import ContentMode, NewsEvent, NewsQuery
from macro_platform.contracts.provider import (
    Dataset,
    FetchContext,
    IngestJobRequest,
    IngestJobResult,
    ProviderCapabilities,
    ProviderPage,
)
from macro_platform.jobs.ingestion_checkpoint import CommittedPage, IngestionCheckpointService
from macro_platform.jobs.runner import IngestionExecutionContext
from macro_platform.normalization.common import canonical_json_checksum, utc_now
from macro_platform.observability.metrics import PROVIDER_REJECTIONS
from macro_platform.providers.base import NewsProvider
from macro_platform.storage.database import Database
from macro_platform.storage.repositories import IngestionCheckpointRepository
from macro_platform.storage.unit_of_work import UnitOfWork

_PAGE_SIZE = 1_000
_NEWS_PAGE_SIZE = 500
_PIT_CLOCK_SKEW = timedelta(seconds=5)


class MacroReleaseProvider(Protocol):
    """Narrow provider seam for a release calendar.

    NBS is intentionally not a generic macro-observation provider, so the
    scheduler depends on only the capability it actually consumes.
    """

    def capabilities(self) -> ProviderCapabilities: ...

    async def fetch_macro_releases(
        self, query: MacroReleaseQuery, context: FetchContext
    ) -> ProviderPage[MacroRelease]: ...


class MacroReleaseIngestHandler:
    """Persist a page from one allowlisted live macro-release calendar."""

    def __init__(
        self,
        provider: MacroReleaseProvider,
        *,
        provider_role: str,
        region: Region,
        timeout_seconds: float,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("macro release ingestion timeout must be positive")
        capabilities = provider.capabilities()
        if Dataset.MACRO_RELEASES not in capabilities.datasets:
            raise ValueError("macro release ingestion provider lacks macro_releases capability")
        if region not in capabilities.regions:
            raise ValueError("macro release ingestion region is not supported by provider")
        self._provider = provider
        self._provider_role = provider_role
        self._region = region
        self._timeout_seconds = timeout_seconds
        self._now = now
        self._capabilities = capabilities

    @property
    def provider_id(self) -> str:
        return self._capabilities.provider_id

    async def run(self, request: IngestJobRequest) -> IngestJobResult:
        raise RuntimeError("macro release ingestion must run through JobRunner with a database")

    async def run_checkpointed(
        self,
        request: IngestJobRequest,
        checkpoints: IngestionCheckpointService,
        database: Database,
        execution: IngestionExecutionContext,
    ) -> IngestJobResult:
        self._validate_request(request)
        started_at = self._now().astimezone(UTC)
        await checkpoints.reject_unsupported_historical_pit(
            database,
            run_id=execution.run_id,
            provider_id=self.provider_id,
            supports_point_in_time=self._capabilities.supports_point_in_time,
            historical_request=request.as_of < started_at - _PIT_CLOCK_SKEW,
        )
        live_as_of = request.as_of + timedelta(seconds=self._timeout_seconds) + _PIT_CLOCK_SKEW
        page: ProviderPage[MacroRelease] = await self._provider.fetch_macro_releases(
            MacroReleaseQuery(
                regions={self._region},
                scheduled_from=request.start,
                scheduled_to=request.end,
                as_of=live_as_of,
                cursor=request.cursor,
                limit=_PAGE_SIZE,
            ),
            FetchContext(
                request_id=uuid4(),
                as_of=live_as_of,
                deadline_at=started_at + timedelta(seconds=self._timeout_seconds),
            ),
        )
        rejected_records = await _persist_provider_rejections(
            checkpoints=checkpoints,
            database=database,
            run_id=execution.run_id,
            provider_id=self.provider_id,
            provider_role=request.provider_role,
            dataset=Dataset.MACRO_RELEASES,
            warnings=page.warnings,
        )
        committed = await _commit_macro_release_page(
            database=database,
            checkpoints=checkpoints,
            execution=execution,
            request=request,
            page=page,
            region=self._region,
        )
        return IngestJobResult(
            run_id=execution.run_id,
            status="partial" if rejected_records else "succeeded",
            provider_role=request.provider_role,
            dataset=Dataset.MACRO_RELEASES,
            started_at=started_at,
            finished_at=self._now().astimezone(UTC),
            records_fetched=len(page.items) + rejected_records,
            records_accepted=len(page.items),
            records_rejected=rejected_records,
            records_inserted=len(page.items) if committed else 0,
            records_updated=0,
            next_cursor=page.next_cursor,
            source_watermark=page.source_watermark,
            warnings=list(page.warnings),
        )

    def _validate_request(self, request: IngestJobRequest) -> None:
        if request.provider_role != self._provider_role:
            raise ValueError("macro release ingestion received an unexpected provider role")
        if request.dataset is not Dataset.MACRO_RELEASES or request.regions != {self._region}:
            raise ValueError(
                "macro release ingestion request does not match its configured dataset"
            )


class NewsIngestHandler:
    """Persist a page from one allowlisted headline-only live news provider."""

    def __init__(
        self,
        provider: NewsProvider,
        *,
        provider_role: str,
        region: Region,
        timeout_seconds: float,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("news ingestion timeout must be positive")
        capabilities = provider.capabilities()
        if Dataset.NEWS not in capabilities.datasets:
            raise ValueError("news ingestion provider lacks news capability")
        if region not in capabilities.regions:
            raise ValueError("news ingestion region is not supported by provider")
        self._provider = provider
        self._provider_role = provider_role
        self._region = region
        self._timeout_seconds = timeout_seconds
        self._now = now
        self._capabilities = capabilities

    @property
    def provider_id(self) -> str:
        return self._capabilities.provider_id

    async def run(self, request: IngestJobRequest) -> IngestJobResult:
        raise RuntimeError("news ingestion must run through JobRunner with a database")

    async def run_checkpointed(
        self,
        request: IngestJobRequest,
        checkpoints: IngestionCheckpointService,
        database: Database,
        execution: IngestionExecutionContext,
    ) -> IngestJobResult:
        self._validate_request(request)
        started_at = self._now().astimezone(UTC)
        await checkpoints.reject_unsupported_historical_pit(
            database,
            run_id=execution.run_id,
            provider_id=self.provider_id,
            supports_point_in_time=self._capabilities.supports_point_in_time,
            historical_request=request.as_of < started_at - _PIT_CLOCK_SKEW,
        )
        live_as_of = request.as_of + timedelta(seconds=self._timeout_seconds) + _PIT_CLOCK_SKEW
        page: ProviderPage[NewsEvent] = await self._provider.fetch_news(
            NewsQuery(
                regions={self._region},
                published_from=request.start,
                published_to=request.end,
                as_of=live_as_of,
                content_mode=ContentMode.SNIPPET,
                cursor=request.cursor,
                limit=_NEWS_PAGE_SIZE,
            ),
            FetchContext(
                request_id=uuid4(),
                as_of=live_as_of,
                deadline_at=started_at + timedelta(seconds=self._timeout_seconds),
            ),
        )
        rejected_records = await _persist_provider_rejections(
            checkpoints=checkpoints,
            database=database,
            run_id=execution.run_id,
            provider_id=self.provider_id,
            provider_role=request.provider_role,
            dataset=Dataset.NEWS,
            warnings=page.warnings,
        )
        committed = await _commit_news_page(
            database=database,
            checkpoints=checkpoints,
            execution=execution,
            request=request,
            page=page,
            region=self._region,
        )
        return IngestJobResult(
            run_id=execution.run_id,
            status="partial" if rejected_records else "succeeded",
            provider_role=request.provider_role,
            dataset=Dataset.NEWS,
            started_at=started_at,
            finished_at=self._now().astimezone(UTC),
            records_fetched=len(page.items) + rejected_records,
            records_accepted=len(page.items),
            records_rejected=rejected_records,
            records_inserted=len(page.items) if committed else 0,
            records_updated=0,
            next_cursor=page.next_cursor,
            source_watermark=page.source_watermark,
            warnings=list(page.warnings),
        )

    def _validate_request(self, request: IngestJobRequest) -> None:
        if request.provider_role != self._provider_role:
            raise ValueError("news ingestion received an unexpected provider role")
        if request.dataset is not Dataset.NEWS or request.regions != {self._region}:
            raise ValueError("news ingestion request does not match its configured dataset")


async def _commit_macro_release_page(
    *,
    database: Database,
    checkpoints: IngestionCheckpointService,
    execution: IngestionExecutionContext,
    request: IngestJobRequest,
    page: ProviderPage[MacroRelease],
    region: Region,
) -> bool:
    committed_page = _committed_page(
        request=request,
        region=region,
        page=page,
        record_ids=[item.source.provider_record_id for item in page.items],
    )
    async with UnitOfWork(database).transaction() as session:
        repository = IngestionCheckpointRepository(session, ingestion_run_id=execution.run_id)

        async def write_records(_: object) -> None:
            series_by_id = {item.series_id: _release_calendar_series(item) for item in page.items}
            for series in series_by_id.values():
                await repository.upsert_macro_series(series)
            for item in page.items:
                await repository.upsert_macro_release(item)

        return await checkpoints.commit_page(repository, committed_page, write_records)


def _release_calendar_series(release: MacroRelease) -> MacroSeries:
    """Supply the FK-backed series identity for a release-calendar-only source."""

    return MacroSeries(
        series_id=release.series_id,
        region=release.region,
        authority=release.source.provider_id,
        code=release.series_id.rsplit(":", maxsplit=1)[-1],
        name=f"{release.region.value} release calendar",
        description="Scheduler-generated series identity for an official release calendar.",
        frequency=Frequency.IRREGULAR,
        unit=release.unit,
        transformation="level",
        seasonal_adjustment="unknown",
        source=release.source,
    )


async def _commit_news_page(
    *,
    database: Database,
    checkpoints: IngestionCheckpointService,
    execution: IngestionExecutionContext,
    request: IngestJobRequest,
    page: ProviderPage[NewsEvent],
    region: Region,
) -> bool:
    committed_page = _committed_page(
        request=request,
        region=region,
        page=page,
        record_ids=[item.source.provider_record_id for item in page.items],
    )
    async with UnitOfWork(database).transaction() as session:
        repository = IngestionCheckpointRepository(session, ingestion_run_id=execution.run_id)

        async def write_records(_: object) -> None:
            for item in page.items:
                await repository.upsert_news_event(item)

        return await checkpoints.commit_page(repository, committed_page, write_records)


def _committed_page[T: MacroRelease | NewsEvent](
    *,
    request: IngestJobRequest,
    region: Region,
    page: ProviderPage[T],
    record_ids: list[str],
) -> CommittedPage:
    return CommittedPage(
        provider_role=request.provider_role,
        dataset=request.dataset,
        region=region.value,
        page_fingerprint=canonical_json_checksum(
            {
                "query": request.model_dump(mode="json"),
                "source_watermark": page.source_watermark,
                "items": [item.model_dump(mode="json") for item in page.items],
            }
        ),
        source_watermark=page.source_watermark,
        next_cursor=page.next_cursor,
        accepted_record_ids=record_ids,
    )


async def _persist_provider_rejections(
    *,
    checkpoints: IngestionCheckpointService,
    database: Database,
    run_id: UUID,
    provider_id: str,
    provider_role: str,
    dataset: Dataset,
    warnings: list[WarningItem],
) -> int:
    """Persist provider quarantine warnings without trusting their raw payloads."""

    rejected_records = 0
    for warning in warnings:
        rejection = warning.details.get("rejection")
        if warning.code != "PROVIDER_RECORD_QUARANTINED" or not isinstance(rejection, dict):
            continue
        error_code = rejection.get("error_code")
        redacted_payload = rejection.get("redacted_payload")
        if not isinstance(error_code, str) or not isinstance(redacted_payload, dict):
            continue
        await checkpoints.record_rejection(
            database,
            run_id=run_id,
            provider_id=provider_id,
            error_code=error_code,
            redacted_payload=redacted_payload,
        )
        PROVIDER_REJECTIONS.labels(
            provider_role=provider_role,
            dataset=dataset.value,
            error_code=error_code,
        ).inc()
        rejected_records += 1
    return rejected_records
