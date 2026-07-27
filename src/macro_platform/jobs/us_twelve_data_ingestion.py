"""Durable ingestion bridge for the approved Twelve Data US daily-bars adapter."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from macro_platform.contracts.common import Region, WarningItem
from macro_platform.contracts.market import Adjustment, BarQuery, Interval, MarketBar
from macro_platform.contracts.provider import (
    Dataset,
    FetchContext,
    IngestJobRequest,
    IngestJobResult,
    ProviderPage,
)
from macro_platform.jobs.ingestion_checkpoint import CommittedPage, IngestionCheckpointService
from macro_platform.jobs.runner import IngestionExecutionContext
from macro_platform.normalization.common import canonical_json_checksum, utc_now
from macro_platform.observability.metrics import PROVIDER_FRESHNESS, PROVIDER_REJECTIONS
from macro_platform.providers.base import ProviderCursorError
from macro_platform.providers.us.twelve_data import (
    TWELVE_DATA_PRIMARY_ROLE,
    TwelveDataDailyBarsProvider,
)
from macro_platform.storage.database import Database
from macro_platform.storage.repositories import IngestionCheckpointRepository
from macro_platform.storage.unit_of_work import UnitOfWork

_PAGE_SIZE = 1000
_PIT_CLOCK_SKEW = timedelta(seconds=5)


class UsTwelveDataIngestHandler:
    """Writes page-committed raw daily bars and NY trading-date audit evidence.

    Scheduling and retry policy remain in #29's worker layer.  This handler is
    deliberately limited to the #26-approved dataset so it cannot turn the
    live adapter into a generic US ingestion escape hatch.
    """

    def __init__(
        self,
        provider: TwelveDataDailyBarsProvider,
        *,
        supports_point_in_time: bool | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._provider = provider
        self._recovery_provider = provider
        self._supports_point_in_time = (
            provider.capabilities().supports_point_in_time
            if supports_point_in_time is None
            else supports_point_in_time
        )
        self._now = now

    @property
    def provider_id(self) -> str:
        return self._provider.provider_id

    def with_recovery_provider(
        self, provider: TwelveDataDailyBarsProvider
    ) -> UsTwelveDataIngestHandler:
        if provider.provider_id != self.provider_id:
            raise ValueError("Twelve Data recovery provider must keep the provider_id")
        self._recovery_provider = provider
        return self

    async def run(self, request: IngestJobRequest) -> IngestJobResult:
        raise RuntimeError("Twelve Data ingestion must run through JobRunner with a database")

    async def run_checkpointed(
        self,
        request: IngestJobRequest,
        checkpoints: IngestionCheckpointService,
        database: Database,
        execution: IngestionExecutionContext,
    ) -> IngestJobResult:
        self._validate_request(request)
        now = self._now().astimezone(UTC)
        await checkpoints.reject_unsupported_historical_pit(
            database,
            run_id=execution.run_id,
            provider_id=self.provider_id,
            supports_point_in_time=self._supports_point_in_time,
            historical_request=request.as_of < now - _PIT_CLOCK_SKEW,
        )
        # Twelve Data does not provide a historical snapshot.  This is a live
        # ingestion deadline, so use a deterministic upper bound for the
        # provider contract while retaining each record's actual first-seen
        # timestamp for durable PIT reads.  Deriving it from request.as_of
        # keeps signed cursors valid across retry/recovery attempts.
        live_as_of = (
            request.as_of
            + timedelta(seconds=self._provider.request_timeout_seconds)
            + _PIT_CLOCK_SKEW
        )
        context = FetchContext(
            request_id=uuid4(),
            as_of=live_as_of,
            deadline_at=now + timedelta(seconds=self._provider.request_timeout_seconds),
        )
        active_provider = self._provider

        async def fetch_page(cursor: str | None) -> ProviderPage[MarketBar]:
            return await active_provider.fetch_bars(
                BarQuery(
                    instrument_ids=list(active_provider.instrument_ids),
                    interval=Interval.D1,
                    start=request.start,
                    end=request.end,
                    adjustment=Adjustment.RAW,
                    as_of=live_as_of,
                    cursor=cursor,
                    limit=_PAGE_SIZE,
                ),
                context,
            )

        recovery_warning: WarningItem | None = None
        effective_cursor = request.cursor
        try:
            page = await fetch_page(effective_cursor)
        except ProviderCursorError as expired_cursor_error:
            async with database.session() as session:
                (
                    recovered_watermark,
                    recovered_cursor,
                ) = await checkpoints.recover_committed_watermark(
                    IngestionCheckpointRepository(session, ingestion_run_id=execution.run_id),
                    provider_role=request.provider_role,
                    dataset=request.dataset,
                    region=Region.US.value,
                )
            if recovered_watermark is None or recovered_cursor is None:
                raise
            active_provider = self._recovery_provider
            effective_cursor = recovered_cursor
            page = await fetch_page(effective_cursor)
            if page.source_watermark != recovered_watermark:
                raise ProviderCursorError(
                    "Twelve Data cursor recovery returned a different source watermark: "
                    f"expected {recovered_watermark}, got {page.source_watermark}",
                    code="CURSOR_RECOVERY_MISMATCH",
                ) from expired_cursor_error
            recovery_warning = WarningItem(
                code="CURSOR_RECOVERED",
                message="expired Twelve Data cursor resumed from committed checkpoint cursor",
                scope=Region.US.value,
                details={"committed_watermark": recovered_watermark},
            )

        for bar in page.items:
            await checkpoints.record_raw_timestamp(
                database,
                run_id=execution.run_id,
                provider_id=self.provider_id,
                raw_value=bar.trading_date.isoformat(),
                raw_timezone="America/New_York",
                normalized_utc=bar.bar_start.astimezone(UTC).isoformat(),
            )
        for warning in page.warnings:
            rejection = warning.details.get("rejection")
            if warning.code != "PROVIDER_RECORD_QUARANTINED" or not isinstance(rejection, dict):
                continue
            error_code = rejection.get("error_code")
            redacted_payload = rejection.get("redacted_payload")
            if not isinstance(error_code, str) or not isinstance(redacted_payload, dict):
                continue
            await checkpoints.record_rejection(
                database,
                run_id=execution.run_id,
                provider_id=self.provider_id,
                error_code=error_code,
                redacted_payload=redacted_payload,
            )
            PROVIDER_REJECTIONS.labels(
                provider_role=TWELVE_DATA_PRIMARY_ROLE,
                dataset=Dataset.BARS.value,
                error_code=error_code,
            ).inc()
        page_fingerprint = canonical_json_checksum(
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
            region=Region.US.value,
            page_fingerprint=page_fingerprint,
            source_watermark=page.source_watermark,
            next_cursor=page.next_cursor,
            accepted_record_ids=[bar.source.provider_record_id for bar in page.items],
        )
        async with UnitOfWork(database).transaction() as session:
            repository = IngestionCheckpointRepository(session, ingestion_run_id=execution.run_id)

            async def write_records(_: object) -> None:
                for instrument in self._provider.instrument_contracts(fetched_at=page.fetched_at):
                    await repository.upsert_instrument(instrument)
                for bar in page.items:
                    await repository.upsert_bar(bar)

            committed = await checkpoints.commit_page(repository, committed_page, write_records)

        rejected_records = sum(
            warning.code == "PROVIDER_RECORD_QUARANTINED" for warning in page.warnings
        )
        warnings = [*page.warnings]
        if recovery_warning is not None:
            warnings.append(recovery_warning)
        finished_at = self._now().astimezone(UTC)
        if page.items:
            PROVIDER_FRESHNESS.labels(
                provider_role=TWELVE_DATA_PRIMARY_ROLE,
                dataset=Dataset.BARS.value,
                region=Region.US.value,
            ).set(max(bar.bar_end for bar in page.items).timestamp())
        return IngestJobResult(
            run_id=execution.run_id,
            status="partial" if rejected_records else "succeeded",
            provider_role=request.provider_role,
            dataset=request.dataset,
            started_at=now,
            finished_at=finished_at,
            records_fetched=len(page.items) + rejected_records,
            records_accepted=len(page.items),
            records_rejected=rejected_records,
            records_inserted=len(page.items) if committed else 0,
            records_updated=0,
            next_cursor=page.next_cursor,
            source_watermark=page.source_watermark,
            warnings=warnings,
        )

    @staticmethod
    def _validate_request(request: IngestJobRequest) -> None:
        if request.provider_role != TWELVE_DATA_PRIMARY_ROLE:
            raise ValueError("Twelve Data ingestion requires the us.market.primary role")
        if request.dataset is not Dataset.BARS:
            raise ValueError("Twelve Data ingestion supports bars only")
        if request.regions != {Region.US}:
            raise ValueError("Twelve Data ingestion requires the US region")
