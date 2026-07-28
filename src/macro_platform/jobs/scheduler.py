"""Independent scheduled-ingestion worker and report-input materialization."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as time_of_day
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text

from macro_platform.config import Settings, get_settings
from macro_platform.contracts.common import Region
from macro_platform.contracts.provider import Dataset, IngestJobRequest, IngestJobResult
from macro_platform.jobs.cn_baostock_ingestion import CnBaoStockIngestHandler
from macro_platform.jobs.hk_xtquant_ingestion import HkXtQuantIngestHandler
from macro_platform.jobs.runner import JobRunner
from macro_platform.jobs.us_twelve_data_ingestion import UsTwelveDataIngestHandler
from macro_platform.normalization.common import utc_now
from macro_platform.observability import configure_logging
from macro_platform.observability.metrics import SCHEDULED_REPORT_RUNS, SCHEDULED_TASK_RUNS
from macro_platform.providers.base import ProviderError
from macro_platform.providers.cn.baostock import BaoStockDailyBarsProvider
from macro_platform.providers.factory import create_provider_registry
from macro_platform.providers.hk.xtquant import HkXtQuantDailyBarsProvider
from macro_platform.providers.registry import ProviderRegistry
from macro_platform.providers.us.twelve_data import TwelveDataDailyBarsProvider
from macro_platform.storage.database import Database
from macro_platform.storage.repositories import (
    ScheduledTaskCheckpoint,
    ScheduledTaskCheckpointRepository,
)
from macro_platform.storage.unit_of_work import UnitOfWork

if TYPE_CHECKING:
    from macro_platform.services.report_input_materializer import MaterializedReportInput

ScheduledTaskStatus = Literal["succeeded", "failed", "retryable"]
ScheduledWorkerStatus = Literal["succeeded", "degraded", "blocked", "retryable", "locked"]


@dataclass(frozen=True, slots=True)
class ScheduledTaskResult:
    task_id: str
    provider_role: str
    status: ScheduledTaskStatus
    dataset: Dataset | None = None
    region: Region | None = None
    record_count: int = 0
    records_rejected: int = 0
    attempt_no: int = 1
    run_id: UUID | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ScheduledWorkerResult:
    report_date: date
    status: ScheduledWorkerStatus
    task_results: tuple[ScheduledTaskResult, ...]
    snapshot_id: str | None = None
    quality_status: Literal["passed", "degraded", "blocked", "retryable"] | None = None


class ScheduledTask(Protocol):
    task_id: str
    required: bool

    async def run(self, report_date: date) -> ScheduledTaskResult: ...


class ReportInputMaterializer(Protocol):
    async def materialize(
        self,
        report_date: date,
        *,
        task_results: tuple[ScheduledTaskResult, ...],
    ) -> MaterializedReportInput: ...


class ScheduledTaskExecutor(Protocol):
    """The durable execution seam; production supplies ``JobRunner``."""

    async def execute(self, request: IngestJobRequest) -> IngestJobResult: ...


class ScheduledTaskCheckpointStore(Protocol):
    """State needed to continue a report-date task after worker restart."""

    async def begin_or_load(
        self,
        *,
        report_date: date,
        task_id: str,
        provider_role: str,
        dataset: Dataset,
        region: str,
        request_as_of: datetime,
    ) -> ScheduledTaskCheckpoint: ...

    async def advance(
        self,
        checkpoint: ScheduledTaskCheckpoint,
        *,
        run_id: UUID,
        next_cursor: str | None,
        source_watermark: str | None,
        records_accepted: int,
        records_rejected: int,
    ) -> ScheduledTaskCheckpoint: ...


class PostgresScheduledTaskCheckpointStore:
    """Transactional adapter around the durable scheduler checkpoint repository."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def begin_or_load(
        self,
        *,
        report_date: date,
        task_id: str,
        provider_role: str,
        dataset: Dataset,
        region: str,
        request_as_of: datetime,
    ) -> ScheduledTaskCheckpoint:
        async with UnitOfWork(self._database).transaction() as session:
            return await ScheduledTaskCheckpointRepository(session).begin_or_load(
                report_date=report_date,
                task_id=task_id,
                provider_role=provider_role,
                dataset=dataset,
                region=region,
                request_as_of=request_as_of,
            )

    async def advance(
        self,
        checkpoint: ScheduledTaskCheckpoint,
        *,
        run_id: UUID,
        next_cursor: str | None,
        source_watermark: str | None,
        records_accepted: int,
        records_rejected: int,
    ) -> ScheduledTaskCheckpoint:
        async with UnitOfWork(self._database).transaction() as session:
            return await ScheduledTaskCheckpointRepository(session).advance(
                checkpoint,
                run_id=run_id,
                next_cursor=next_cursor,
                source_watermark=source_watermark,
                records_accepted=records_accepted,
                records_rejected=records_rejected,
            )


ScheduledRequestFactory = Callable[[date, datetime, str | None], IngestJobRequest]


class CheckpointedScheduledTask:
    """Run every page through ``JobRunner`` and retain resumable task state.

    Page commits protect normalized facts.  The task checkpoint additionally
    retains the original request clock and opaque provider cursor, which is
    necessary to continue signed cursors after a worker restart.
    """

    def __init__(
        self,
        *,
        task_id: str,
        required: bool,
        provider_role: str,
        dataset: Dataset,
        region: Region,
        executor: ScheduledTaskExecutor,
        checkpoint_store: ScheduledTaskCheckpointStore,
        request_factory: ScheduledRequestFactory,
        now: Callable[[], datetime],
        request_as_of_lead: timedelta = timedelta(days=1),
    ) -> None:
        if not task_id.strip() or not provider_role.strip():
            raise ValueError("scheduled task and provider role must not be empty")
        if request_as_of_lead <= timedelta(0):
            raise ValueError("scheduled task request_as_of_lead must be positive")
        self.task_id = task_id
        self.required = required
        self.provider_role = provider_role
        self._dataset = dataset
        self._region = region
        self._executor = executor
        self._checkpoint_store = checkpoint_store
        self._request_factory = request_factory
        self._now = now
        self._request_as_of_lead = request_as_of_lead

    async def run(self, report_date: date) -> ScheduledTaskResult:
        checkpoint = await self._checkpoint_store.begin_or_load(
            report_date=report_date,
            task_id=self.task_id,
            provider_role=self.provider_role,
            dataset=self._dataset,
            region=self._region.value,
            # The live providers reject a historical PIT timestamp.  This is
            # not report PIT: it is a persisted request ceiling used only to
            # retain the signed-cursor query identity across recovery.
            request_as_of=self._now().astimezone(UTC) + self._request_as_of_lead,
        )
        if checkpoint.status == "completed":
            return _scheduled_task_result_from_checkpoint(checkpoint)

        while True:
            request = self._request_factory(
                report_date,
                checkpoint.request_as_of,
                checkpoint.next_cursor,
            )
            try:
                result = await self._executor.execute(request)
            except ProviderError as error:
                return ScheduledTaskResult(
                    task_id=self.task_id,
                    provider_role=self.provider_role,
                    dataset=self._dataset,
                    region=self._region,
                    status="retryable" if error.retryable else "failed",
                    error_code=error.code,
                )
            if result.provider_role != self.provider_role or result.dataset is not self._dataset:
                raise ValueError("scheduled task received a mismatched durable ingestion result")
            if result.status == "retry_wait":
                return ScheduledTaskResult(
                    task_id=self.task_id,
                    provider_role=self.provider_role,
                    dataset=self._dataset,
                    region=self._region,
                    status="retryable",
                    run_id=result.run_id,
                    error_code=result.error_code,
                )
            if result.status == "failed":
                return ScheduledTaskResult(
                    task_id=self.task_id,
                    provider_role=self.provider_role,
                    dataset=self._dataset,
                    region=self._region,
                    status="failed",
                    run_id=result.run_id,
                    error_code=result.error_code,
                )
            checkpoint = await self._checkpoint_store.advance(
                checkpoint,
                run_id=result.run_id,
                next_cursor=result.next_cursor,
                source_watermark=result.source_watermark,
                records_accepted=result.records_accepted,
                records_rejected=result.records_rejected,
            )
            if checkpoint.status == "completed":
                return _scheduled_task_result_from_checkpoint(checkpoint)


def _scheduled_task_result_from_checkpoint(
    checkpoint: ScheduledTaskCheckpoint,
) -> ScheduledTaskResult:
    if checkpoint.run_id is None:
        raise RuntimeError("completed scheduled task checkpoint is missing its durable run ID")
    return ScheduledTaskResult(
        task_id=checkpoint.task_id,
        provider_role=checkpoint.provider_role,
        dataset=checkpoint.dataset,
        region=Region(checkpoint.region),
        status="succeeded",
        record_count=checkpoint.records_accepted,
        records_rejected=checkpoint.records_rejected,
        run_id=checkpoint.run_id,
    )


class ReportDateLock(Protocol):
    def hold(self, report_date: date) -> AbstractAsyncContextManager[bool]: ...


class RetryableScheduledTaskError(RuntimeError):
    """A task failure that may be retried with bounded backoff."""


class SchedulerNotConfiguredError(RuntimeError):
    """The worker was started before its production schedule was defined."""


class PostgresReportDateLock:
    """Session-scoped PostgreSQL advisory lock for a complete report date."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @asynccontextmanager
    async def hold(self, report_date: date) -> AsyncIterator[bool]:
        lock_key = _report_date_lock_key(report_date)
        async with self._database.engine.connect() as connection:
            acquired = bool(
                (
                    await connection.execute(
                        text("SELECT pg_try_advisory_lock(:lock_key)"),
                        {"lock_key": lock_key},
                    )
                ).scalar_one()
            )
            try:
                yield acquired
            finally:
                if acquired:
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(:lock_key)"),
                        {"lock_key": lock_key},
                    )


class ScheduledIngestionWorker:
    """Run a date-scoped task bundle with exclusion, bounded retry and backfill."""

    def __init__(
        self,
        *,
        tasks: Sequence[ScheduledTask],
        report_date_lock: ReportDateLock,
        max_attempts: int = 3,
        retry_delay_seconds: float = 1.0,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        input_materializer: ReportInputMaterializer | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if retry_delay_seconds <= 0:
            raise ValueError("retry_delay_seconds must be positive")
        task_ids = [task.task_id for task in tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("scheduled task IDs must be unique")
        self._tasks = tuple(tasks)
        self._report_date_lock = report_date_lock
        self._max_attempts = max_attempts
        self._retry_delay_seconds = retry_delay_seconds
        self._sleeper = sleeper
        self._input_materializer = input_materializer
        self._logger = structlog.get_logger("scheduled_ingestion_worker")

    async def run_for_date(self, report_date: date) -> ScheduledWorkerResult:
        started = time.monotonic()
        async with self._report_date_lock.hold(report_date) as acquired:
            if not acquired:
                result = ScheduledWorkerResult(report_date, "locked", ())
                SCHEDULED_REPORT_RUNS.labels(status=result.status).inc()
                await self._logger.ainfo(
                    "scheduled_report_locked",
                    action="scheduled_report",
                    run_id=None,
                    provider_role=None,
                    dataset=None,
                    region=None,
                    report_date=report_date.isoformat(),
                    attempt_no=None,
                    terminal=result.status,
                    duration_ms=_duration_ms(started),
                    record_count=0,
                    error_code=None,
                )
                return result

            task_results = tuple([await self._run_task(task, report_date) for task in self._tasks])
            status = _worker_status(task_results, self._tasks)
            snapshot_id: str | None = None
            quality_status: Literal["passed", "degraded", "blocked", "retryable"] | None = None
            materialization_error_code: str | None = None
            if self._input_materializer is not None:
                try:
                    materialized = await self._input_materializer.materialize(
                        report_date,
                        task_results=task_results,
                    )
                    snapshot_id = materialized.snapshot.snapshot_id
                    quality_status = materialized.quality.status
                    status = _combine_worker_and_quality_status(status, quality_status)
                except Exception as error:
                    status = "blocked"
                    materialization_error_code = type(error).__name__.upper()
            run_ids = sorted(str(task.run_id) for task in task_results if task.run_id is not None)
            result = ScheduledWorkerResult(
                report_date,
                status,
                task_results,
                snapshot_id=snapshot_id,
                quality_status=quality_status,
            )
            SCHEDULED_REPORT_RUNS.labels(status=status).inc()
            await self._logger.ainfo(
                "scheduled_report_finished",
                action="scheduled_report",
                run_id=run_ids[0] if len(run_ids) == 1 else None,
                provider_role=None,
                dataset=None,
                region=None,
                report_date=report_date.isoformat(),
                attempt_no=None,
                terminal=status,
                duration_ms=_duration_ms(started),
                record_count=sum(task.record_count for task in task_results),
                error_code=materialization_error_code,
                task_count=len(task_results),
                run_ids=run_ids,
                snapshot_id=snapshot_id,
                quality_status=quality_status,
            )
            return result

    async def backfill(self, start_date: date, end_date: date) -> tuple[ScheduledWorkerResult, ...]:
        if end_date < start_date:
            raise ValueError("backfill end_date must not be before start_date")
        results: list[ScheduledWorkerResult] = []
        report_date = start_date
        while report_date <= end_date:
            results.append(await self.run_for_date(report_date))
            report_date += timedelta(days=1)
        return tuple(results)

    async def _run_task(self, task: ScheduledTask, report_date: date) -> ScheduledTaskResult:
        provider_role = _task_provider_role(task)
        started = time.monotonic()
        for attempt_no in range(1, self._max_attempts + 1):
            try:
                task_result = await task.run(report_date)
                if task_result.task_id != task.task_id:
                    raise ValueError("scheduled task returned a mismatched task ID")
                if task_result.status == "retryable" and attempt_no < self._max_attempts:
                    await self._retry_task(
                        task=task,
                        report_date=report_date,
                        provider_role=task_result.provider_role,
                        attempt_no=attempt_no,
                        error_code=task_result.error_code,
                    )
                    continue
                if task_result.status == "succeeded" and not task_result.run_id:
                    result = ScheduledTaskResult(
                        task_id=task_result.task_id,
                        provider_role=task_result.provider_role,
                        status="failed",
                        dataset=task_result.dataset,
                        region=task_result.region,
                        record_count=task_result.record_count,
                        records_rejected=task_result.records_rejected,
                        attempt_no=attempt_no,
                        error_code="MISSING_DURABLE_RUN_ID",
                    )
                else:
                    result = replace(task_result, attempt_no=attempt_no)
            except RetryableScheduledTaskError as error:
                if attempt_no < self._max_attempts:
                    await self._retry_task(
                        task=task,
                        report_date=report_date,
                        provider_role=provider_role,
                        attempt_no=attempt_no,
                        error_code=type(error).__name__,
                    )
                    continue
                result = ScheduledTaskResult(
                    task_id=task.task_id,
                    provider_role=provider_role,
                    status="retryable",
                    attempt_no=attempt_no,
                    error_code=type(error).__name__,
                )
            except Exception as error:
                result = ScheduledTaskResult(
                    task_id=task.task_id,
                    provider_role=provider_role,
                    status="failed",
                    attempt_no=attempt_no,
                    error_code=type(error).__name__,
                )
            SCHEDULED_TASK_RUNS.labels(
                task_id=result.task_id,
                provider_role=result.provider_role,
                status=result.status,
            ).inc()
            await self._logger.ainfo(
                "scheduled_task_finished",
                action="scheduled_task",
                report_date=report_date.isoformat(),
                task_id=result.task_id,
                provider_role=result.provider_role,
                dataset=result.dataset.value if result.dataset is not None else None,
                region=result.region.value if result.region is not None else None,
                attempt_no=result.attempt_no,
                run_id=str(result.run_id) if result.run_id is not None else None,
                terminal=result.status,
                duration_ms=_duration_ms(started),
                record_count=result.record_count,
                error_code=result.error_code,
            )
            return result
        raise AssertionError("retry loop must return a task result")

    async def _retry_task(
        self,
        *,
        task: ScheduledTask,
        report_date: date,
        provider_role: str,
        attempt_no: int,
        error_code: str | None,
    ) -> None:
        await self._logger.awarning(
            "scheduled_task_retrying",
            action="scheduled_task",
            run_id=None,
            provider_role=provider_role,
            dataset=None,
            region=None,
            report_date=report_date.isoformat(),
            task_id=task.task_id,
            attempt_no=attempt_no,
            terminal="retry_wait",
            duration_ms=None,
            record_count=0,
            error_code=error_code,
        )
        await self._sleeper(self._retry_delay_seconds * (2 ** (attempt_no - 1)))


def build_registered_tasks(
    *,
    settings: Settings,
    database: Database,
    provider_registry: ProviderRegistry,
    now: Callable[[], datetime],
) -> tuple[ScheduledTask, ...]:
    """Bind the reviewed live daily-bar providers to durable report-date tasks.

    News and macro do not yet have live checkpointed handlers.  They are not
    silently represented by fake tasks: the input materializer will mark their
    required inputs missing until their own ingestion issues land.
    """

    if settings.provider_mode != "live":
        return ()
    provider_registry.assert_production_safe()
    checkpoint_store = PostgresScheduledTaskCheckpointStore(database)
    calendar_timezone = ZoneInfo(settings.worker_schedule_timezone)
    cn_provider = provider_registry.resolve("cn.bars.primary")
    if not isinstance(cn_provider, BaoStockDailyBarsProvider):
        raise TypeError("cn.bars.primary must resolve to BaoStockDailyBarsProvider")
    hk_provider = provider_registry.resolve("hk.bars.primary")
    if not isinstance(hk_provider, HkXtQuantDailyBarsProvider):
        raise TypeError("hk.bars.primary must resolve to HkXtQuantDailyBarsProvider")
    us_provider = provider_registry.resolve("us.market.primary")
    if not isinstance(us_provider, TwelveDataDailyBarsProvider):
        raise TypeError("us.market.primary must resolve to TwelveDataDailyBarsProvider")
    return (
        _daily_bar_task(
            task_id="cn.daily-bars",
            provider_role="cn.bars.primary",
            region=Region.CN,
            executor=JobRunner(
                CnBaoStockIngestHandler(cn_provider, now=now), database=database, now=now
            ),
            checkpoint_store=checkpoint_store,
            calendar_timezone=calendar_timezone,
            lookback_days=settings.worker_bar_lookback_days,
            now=now,
        ),
        _daily_bar_task(
            task_id="hk.daily-bars",
            provider_role="hk.bars.primary",
            region=Region.HK,
            executor=JobRunner(
                HkXtQuantIngestHandler(hk_provider, now=now), database=database, now=now
            ),
            checkpoint_store=checkpoint_store,
            calendar_timezone=calendar_timezone,
            lookback_days=settings.worker_bar_lookback_days,
            now=now,
        ),
        _daily_bar_task(
            task_id="us.daily-bars",
            provider_role="us.market.primary",
            region=Region.US,
            executor=JobRunner(
                UsTwelveDataIngestHandler(us_provider, now=now), database=database, now=now
            ),
            checkpoint_store=checkpoint_store,
            calendar_timezone=calendar_timezone,
            lookback_days=settings.worker_bar_lookback_days,
            now=now,
        ),
    )


def _daily_bar_task(
    *,
    task_id: str,
    provider_role: str,
    region: Region,
    executor: ScheduledTaskExecutor,
    checkpoint_store: ScheduledTaskCheckpointStore,
    calendar_timezone: ZoneInfo,
    lookback_days: int,
    now: Callable[[], datetime],
) -> CheckpointedScheduledTask:
    return CheckpointedScheduledTask(
        task_id=task_id,
        required=True,
        provider_role=provider_role,
        dataset=Dataset.BARS,
        region=region,
        executor=executor,
        checkpoint_store=checkpoint_store,
        request_factory=_daily_bar_request_factory(
            provider_role=provider_role,
            region=region,
            calendar_timezone=calendar_timezone,
            lookback_days=lookback_days,
        ),
        now=now,
    )


def _daily_bar_request_factory(
    *,
    provider_role: str,
    region: Region,
    calendar_timezone: ZoneInfo,
    lookback_days: int,
) -> ScheduledRequestFactory:
    def build(report_date: date, as_of: datetime, cursor: str | None) -> IngestJobRequest:
        return _daily_bar_request(
            report_date=report_date,
            provider_role=provider_role,
            region=region,
            as_of=as_of,
            cursor=cursor,
            calendar_timezone=calendar_timezone,
            lookback_days=lookback_days,
        )

    return build


async def run_scheduler(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    provider_registry: ProviderRegistry | None = None,
    now: Callable[[], datetime] = utc_now,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_schedule_runs: int | None = None,
    run_once_report_date: date | None = None,
    backfill_start_date: date | None = None,
    backfill_end_date: date | None = None,
) -> None:
    """Start the production worker, a one-off report date, or a safe backfill."""

    resolved_settings = settings or get_settings()
    logger = structlog.get_logger("scheduler")
    resolved_run_once_date = run_once_report_date or resolved_settings.worker_run_once_report_date
    resolved_backfill_start = backfill_start_date or resolved_settings.worker_backfill_start_date
    resolved_backfill_end = backfill_end_date or resolved_settings.worker_backfill_end_date
    if resolved_run_once_date is not None and resolved_backfill_start is not None:
        raise ValueError("run-once and backfill modes cannot be combined")
    if (resolved_backfill_start is None) != (resolved_backfill_end is None):
        raise ValueError("backfill start and end dates must be provided together")
    if (
        resolved_backfill_start is not None
        and resolved_backfill_end is not None
        and resolved_backfill_end < resolved_backfill_start
    ):
        raise ValueError("backfill end date must not be before start date")
    owns_database = database is None
    resolved_database = database or Database(resolved_settings.database_url)
    owns_registry = provider_registry is None
    resolved_registry = provider_registry or create_provider_registry(resolved_settings)
    try:
        registered_tasks = build_registered_tasks(
            settings=resolved_settings,
            database=resolved_database,
            provider_registry=resolved_registry,
            now=now,
        )
        if not registered_tasks:
            await logger.aerror(
                "scheduler_not_configured",
                action="scheduler_startup",
                run_id=None,
                provider_role=None,
                dataset=None,
                region=None,
                report_date=None,
                attempt_no=None,
                terminal="blocked",
                duration_ms=0,
                record_count=0,
                error_code="SCHEDULER_NOT_CONFIGURED",
                registered_jobs=0,
            )
            raise SchedulerNotConfiguredError("live scheduled provider tasks are not configured")
        from macro_platform.services.report_input_materializer import (
            PostgresReportInputEvidenceStore,
            PostgresReportInputSnapshotStore,
            ReportInputSnapshotMaterializer,
        )

        schedule_timezone = ZoneInfo(resolved_settings.worker_schedule_timezone)
        worker = ScheduledIngestionWorker(
            tasks=registered_tasks,
            report_date_lock=PostgresReportDateLock(resolved_database),
            max_attempts=resolved_settings.worker_max_task_attempts,
            retry_delay_seconds=resolved_settings.worker_retry_delay_seconds,
            sleeper=sleeper,
            input_materializer=ReportInputSnapshotMaterializer(
                evidence_store=PostgresReportInputEvidenceStore(
                    resolved_database,
                    market_max_age=timedelta(hours=resolved_settings.worker_market_freshness_hours),
                    news_max_age=timedelta(hours=resolved_settings.worker_news_freshness_hours),
                    macro_max_age=timedelta(days=resolved_settings.worker_macro_freshness_days),
                ),
                snapshot_store=PostgresReportInputSnapshotStore(resolved_database),
                now=now,
                cutoff_at=lambda report_date: _report_cutoff_at(
                    report_date=report_date,
                    timezone=schedule_timezone,
                    hour=resolved_settings.worker_report_cutoff_hour_local,
                    minute=resolved_settings.worker_report_cutoff_minute_local,
                ),
            ),
        )
        if resolved_backfill_start is not None and resolved_backfill_end is not None:
            await worker.backfill(resolved_backfill_start, resolved_backfill_end)
            return
        if resolved_run_once_date is not None:
            await worker.run_for_date(resolved_run_once_date)
            return
        await _run_configured_schedule(
            worker,
            timezone=schedule_timezone,
            hour=resolved_settings.worker_schedule_hour_local,
            minute=resolved_settings.worker_schedule_minute_local,
            poll_seconds=resolved_settings.worker_schedule_poll_seconds,
            now=now,
            sleeper=sleeper,
            max_schedule_runs=max_schedule_runs,
        )
    finally:
        if owns_registry:
            await resolved_registry.close()
        if owns_database:
            await resolved_database.dispose()


async def _run_configured_schedule(
    worker: ScheduledIngestionWorker,
    *,
    timezone: ZoneInfo,
    hour: int,
    minute: int,
    poll_seconds: int,
    now: Callable[[], datetime],
    sleeper: Callable[[float], Awaitable[None]],
    max_schedule_runs: int | None,
) -> None:
    completed_dates: set[date] = set()
    schedule_runs = 0
    schedule_time = time_of_day(hour=hour, minute=minute)
    while max_schedule_runs is None or schedule_runs < max_schedule_runs:
        local_now = now().astimezone(timezone)
        if local_now.timetz().replace(tzinfo=None) >= schedule_time:
            report_date = local_now.date()
            if report_date not in completed_dates:
                await worker.run_for_date(report_date)
                completed_dates.add(report_date)
                schedule_runs += 1
                continue
        await sleeper(poll_seconds)


def _daily_bar_request(
    *,
    report_date: date,
    provider_role: str,
    region: Region,
    as_of: datetime,
    cursor: str | None,
    calendar_timezone: ZoneInfo,
    lookback_days: int,
) -> IngestJobRequest:
    end = datetime.combine(report_date, time_of_day.min, tzinfo=calendar_timezone).astimezone(UTC)
    return IngestJobRequest(
        provider_role=provider_role,
        dataset=Dataset.BARS,
        regions={region},
        start=end - timedelta(days=lookback_days),
        end=end,
        as_of=as_of,
        cursor=cursor,
    )


def _report_cutoff_at(*, report_date: date, timezone: ZoneInfo, hour: int, minute: int) -> datetime:
    return datetime.combine(report_date, time_of_day(hour=hour, minute=minute), tzinfo=timezone)


def _task_provider_role(task: ScheduledTask) -> str:
    value = getattr(task, "provider_role", task.task_id)
    return value if isinstance(value, str) and value else task.task_id


def _worker_status(
    results: tuple[ScheduledTaskResult, ...], tasks: tuple[ScheduledTask, ...]
) -> ScheduledWorkerStatus:
    if not tasks:
        return "blocked"
    required_by_task_id = {task.task_id: task.required for task in tasks}
    if any(result.status == "failed" and required_by_task_id[result.task_id] for result in results):
        return "blocked"
    if any(
        result.status == "retryable" and required_by_task_id[result.task_id] for result in results
    ):
        return "retryable"
    if any(result.status != "succeeded" for result in results):
        return "degraded"
    return "succeeded"


def _combine_worker_and_quality_status(
    worker_status: ScheduledWorkerStatus,
    quality_status: Literal["passed", "degraded", "blocked", "retryable"],
) -> ScheduledWorkerStatus:
    if worker_status in {"locked", "blocked"} or quality_status == "blocked":
        return "blocked" if worker_status != "locked" else "locked"
    if worker_status == "retryable" or quality_status == "retryable":
        return "retryable"
    if worker_status == "degraded" or quality_status == "degraded":
        return "degraded"
    return "succeeded"


def _report_date_lock_key(report_date: date) -> int:
    digest = hashlib.sha256(
        f"macro-data-platform:scheduled-report:{report_date.isoformat()}".encode()
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _duration_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the macro-data scheduled ingestion worker")
    parser.add_argument("--report-date", type=_parse_report_date, help="run one ISO report date")
    parser.add_argument(
        "--backfill-start", type=_parse_report_date, help="inclusive ISO start date"
    )
    parser.add_argument("--backfill-end", type=_parse_report_date, help="inclusive ISO end date")
    arguments = parser.parse_args()
    if arguments.report_date is not None and arguments.backfill_start is not None:
        parser.error("--report-date cannot be combined with --backfill-start/--backfill-end")
    if (arguments.backfill_start is None) != (arguments.backfill_end is None):
        parser.error("--backfill-start and --backfill-end must be provided together")
    settings = get_settings()
    configure_logging(settings.log_level)
    asyncio.run(
        run_scheduler(
            settings=settings,
            run_once_report_date=arguments.report_date,
            backfill_start_date=arguments.backfill_start,
            backfill_end_date=arguments.backfill_end,
        )
    )


def _parse_report_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("report dates must use YYYY-MM-DD") from error


if __name__ == "__main__":
    main()
