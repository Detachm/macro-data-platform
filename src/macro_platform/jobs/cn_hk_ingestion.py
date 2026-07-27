from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from macro_platform.contracts.common import WarningItem
from macro_platform.contracts.market import MarketObservation, MarketObservationQuery
from macro_platform.contracts.provider import (
    Dataset,
    FetchContext,
    IngestJobRequest,
    IngestJobResult,
    ProviderPage,
)
from macro_platform.governance.source_policy import IngestionRetentionPolicy, RetentionRule
from macro_platform.jobs.ingestion_checkpoint import CommittedPage, IngestionCheckpointService
from macro_platform.normalization.common import canonical_json_checksum
from macro_platform.providers._regional_fixture import RegionalFixtureProvider
from macro_platform.providers.base import ProviderCursorError
from macro_platform.storage.database import Database
from macro_platform.storage.repositories import IngestionCheckpointRepository
from macro_platform.storage.unit_of_work import UnitOfWork


class CnHkFixtureIngestHandler:
    """Concrete CN/HK fixture ingestion path with durable checkpoint lifecycle."""

    def __init__(
        self, provider: RegionalFixtureProvider, *, supports_point_in_time: bool | None = None
    ) -> None:
        self._provider = provider
        self._supports_point_in_time = (
            provider.capabilities().supports_point_in_time
            if supports_point_in_time is None
            else supports_point_in_time
        )
        self._recovery_provider = provider
        self._retention_policy: IngestionRetentionPolicy | None = None
        self._durable_run_id: UUID | None = None

    def with_recovery_provider(self, provider: RegionalFixtureProvider) -> CnHkFixtureIngestHandler:
        self._recovery_provider = provider
        return self

    @property
    def provider_id(self) -> str:
        return self._provider.provider_id

    def set_retention_policy(self, policy: IngestionRetentionPolicy) -> None:
        self._retention_policy = policy

    def set_durable_run_id(self, run_id: UUID) -> None:
        self._durable_run_id = run_id

    async def run(self, request: IngestJobRequest) -> IngestJobResult:
        raise RuntimeError("CN/HK ingestion must be run through JobRunner with a database")

    async def run_checkpointed(
        self,
        request: IngestJobRequest,
        checkpoints: IngestionCheckpointService,
        database: Database,
    ) -> IngestJobResult:
        if request.dataset is not Dataset.MARKET_OBSERVATIONS:
            raise ValueError("CN/HK fixture ingestion currently supports market_observations only")
        if request.regions != self._provider.region_set():
            raise ValueError("ingest request regions do not match provider")
        if self._durable_run_id is None:
            raise RuntimeError("CN/HK ingestion requires a durable run ID from JobRunner")
        run_id = self._durable_run_id
        self._durable_run_id = None
        await checkpoints.reject_unsupported_historical_pit(
            database,
            run_id=run_id,
            provider_id=self._provider.provider_id,
            supports_point_in_time=self._supports_point_in_time,
            historical_request=request.as_of < datetime.now(UTC),
        )
        context = FetchContext(request_id=uuid4(), as_of=request.as_of, deadline_at=request.end)

        active_provider = self._provider

        async def fetch_page(cursor: str | None) -> ProviderPage[MarketObservation]:
            return await active_provider.fetch_market_observations(
                MarketObservationQuery(
                    regions=request.regions,
                    metric_codes=["market.turnover"],
                    start=request.start,
                    end=request.end,
                    as_of=request.as_of,
                    cursor=cursor,
                ),
                context,
            )

        recovery_warning: WarningItem | None = None
        effective_cursor = request.cursor
        try:
            page = await fetch_page(effective_cursor)
        except ProviderCursorError as expired_cursor_error:
            async with database.session() as session:
                recovered_watermark, _ = await checkpoints.recover_committed_watermark(
                    IngestionCheckpointRepository(session, ingestion_run_id=run_id),
                    provider_role=request.provider_role,
                    dataset=request.dataset,
                    region=self._provider.region.value,
                )
            if recovered_watermark is None:
                raise
            active_provider = self._recovery_provider
            # The expired opaque cursor cannot be replayed.  Resume from the
            # committed source snapshot, whose effective query is cursorless.
            effective_cursor = None
            page = await fetch_page(effective_cursor)
            if page.source_watermark != recovered_watermark:
                raise ProviderCursorError(
                    "cursor recovery returned a different source watermark: "
                    f"expected {recovered_watermark}, got {page.source_watermark}",
                    code="CURSOR_RECOVERY_MISMATCH",
                ) from expired_cursor_error
            recovery_warning = WarningItem(
                code="CURSOR_RECOVERED",
                message="expired provider cursor resumed from committed watermark",
                scope=self._provider.region.value,
                details={"committed_watermark": recovered_watermark},
            )
        for item in page.items:
            raw_value, raw_timezone = active_provider.raw_market_observation_time(
                item.source.provider_record_id
            )
            await checkpoints.record_raw_timestamp(
                database,
                run_id=run_id,
                provider_id=self._provider.provider_id,
                raw_value=raw_value,
                raw_timezone=raw_timezone,
                normalized_utc=item.observed_at.astimezone(UTC).isoformat(),
            )
        fingerprint = canonical_json_checksum(
            {
                "query": request.model_copy(update={"cursor": effective_cursor}).model_dump(
                    mode="json"
                ),
                "source_watermark": page.source_watermark,
                "items": [item.model_dump(mode="json") for item in page.items],
            }
        )
        committed_page = CommittedPage(
            provider_role=request.provider_role,
            dataset=request.dataset,
            region=self._provider.region.value,
            page_fingerprint=fingerprint,
            source_watermark=page.source_watermark,
            next_cursor=page.next_cursor,
            accepted_record_ids=[item.source.provider_record_id for item in page.items],
        )
        retention_rule = (
            RetentionRule.CANONICAL_FACTS
            if self._retention_policy is None
            else self._retention_policy.rule_for(self._provider.region)
        )
        async with UnitOfWork(database).transaction() as session:
            repository = IngestionCheckpointRepository(session, ingestion_run_id=run_id)

            async def write_records(_: object) -> None:
                if retention_rule is not RetentionRule.CANONICAL_FACTS:
                    return
                for item in page.items:
                    await repository.upsert_market_observation(item)

            committed = await checkpoints.commit_page(repository, committed_page, write_records)
        now = datetime.now(UTC)
        return IngestJobResult(
            run_id=run_id,
            status="succeeded",
            provider_role=request.provider_role,
            dataset=request.dataset,
            started_at=now,
            finished_at=now,
            records_fetched=len(page.items),
            records_accepted=len(page.items),
            records_rejected=0,
            records_inserted=(
                len(page.items)
                if committed and retention_rule is RetentionRule.CANONICAL_FACTS
                else 0
            ),
            records_updated=0,
            next_cursor=page.next_cursor,
            source_watermark=page.source_watermark,
            warnings=[] if recovery_warning is None else [recovery_warning],
        )
