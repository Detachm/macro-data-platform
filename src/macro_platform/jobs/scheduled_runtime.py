"""Production composition root and CLI for the scheduled-ingestion worker."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from datetime import time as time_of_day
from zoneinfo import ZoneInfo

import httpx
import structlog

from macro_platform.config import Settings, get_settings
from macro_platform.contracts.common import Region
from macro_platform.contracts.provider import Dataset, IngestJobRequest
from macro_platform.jobs.cn_baostock_ingestion import CnBaoStockIngestHandler
from macro_platform.jobs.hk_xtquant_ingestion import HkXtQuantIngestHandler
from macro_platform.jobs.news_macro_ingestion import MacroReleaseIngestHandler, NewsIngestHandler
from macro_platform.jobs.runner import JobRunner
from macro_platform.jobs.scheduled_tasks import (
    CheckpointedScheduledTask,
    PostgresScheduledTaskCheckpointStore,
)
from macro_platform.jobs.scheduled_types import (
    ScheduledReportWorkflow,
    ScheduledRequestFactory,
    ScheduledTask,
    ScheduledTaskCheckpointStore,
    ScheduledTaskExecutor,
)
from macro_platform.jobs.scheduled_worker import (
    PostgresReportDateLock,
    ScheduledIngestionWorker,
    SchedulerNotConfiguredError,
)
from macro_platform.jobs.us_twelve_data_ingestion import UsTwelveDataIngestHandler
from macro_platform.normalization.common import utc_now
from macro_platform.providers.cn.baostock import BaoStockDailyBarsProvider
from macro_platform.providers.cn.live import CnNbsNewsProvider, CnNbsReleaseProvider
from macro_platform.providers.factory import create_provider_registry
from macro_platform.providers.hk.live import HkmaPressReleaseProvider
from macro_platform.providers.hk.xtquant import HkXtQuantDailyBarsProvider
from macro_platform.providers.registry import ProviderRegistry
from macro_platform.providers.us.twelve_data import TwelveDataDailyBarsProvider
from macro_platform.services.daily_workflow import (
    DailyReportWorkflow,
    PostgresReportGenerationStore,
    build_generation_service,
)
from macro_platform.services.llm import LlmClient
from macro_platform.services.report_delivery import (
    ConfiguredFeishuDelivery,
    PostgresReportDeliveryStore,
)
from macro_platform.services.workflow_alerts import (
    ConfiguredFeishuAlerts,
    PostgresWorkflowAlertStore,
)
from macro_platform.storage.database import Database


def build_registered_tasks(
    *,
    settings: Settings,
    database: Database,
    provider_registry: ProviderRegistry,
    now: Callable[[], datetime],
) -> tuple[ScheduledTask, ...]:
    """Bind all currently approved live report-input providers to durable tasks.

    No synthetic source is registered for incomplete global macro calendar
    coverage. Its absence remains explicit materialized quality evidence and
    blocks a report rather than fabricating a successful input.
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
    cn_macro_provider = provider_registry.resolve("cn.macro.primary")
    if not isinstance(cn_macro_provider, CnNbsReleaseProvider):
        raise TypeError("cn.macro.primary must resolve to CnNbsReleaseProvider")
    cn_news_provider = provider_registry.resolve("cn.news.primary")
    if not isinstance(cn_news_provider, CnNbsNewsProvider):
        raise TypeError("cn.news.primary must resolve to CnNbsNewsProvider")
    hk_news_provider = provider_registry.resolve("hk.news.primary")
    if not isinstance(hk_news_provider, HkmaPressReleaseProvider):
        raise TypeError("hk.news.primary must resolve to HkmaPressReleaseProvider")
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
            required=False,
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
        _macro_release_task(
            task_id="cn.macro-release-calendar",
            provider_role="cn.macro.primary",
            region=Region.CN,
            executor=JobRunner(
                MacroReleaseIngestHandler(
                    cn_macro_provider,
                    provider_role="cn.macro.primary",
                    region=Region.CN,
                    timeout_seconds=settings.provider_timeout_seconds,
                    now=now,
                ),
                database=database,
                now=now,
            ),
            checkpoint_store=checkpoint_store,
            calendar_timezone=calendar_timezone,
            now=now,
        ),
        _news_task(
            task_id="cn.official-headlines",
            provider_role="cn.news.primary",
            region=Region.CN,
            executor=JobRunner(
                NewsIngestHandler(
                    cn_news_provider,
                    provider_role="cn.news.primary",
                    region=Region.CN,
                    timeout_seconds=settings.provider_timeout_seconds,
                    now=now,
                ),
                database=database,
                now=now,
            ),
            checkpoint_store=checkpoint_store,
            calendar_timezone=calendar_timezone,
            now=now,
        ),
        _news_task(
            task_id="hk.official-headlines",
            provider_role="hk.news.primary",
            region=Region.HK,
            executor=JobRunner(
                NewsIngestHandler(
                    hk_news_provider,
                    provider_role="hk.news.primary",
                    region=Region.HK,
                    timeout_seconds=settings.provider_timeout_seconds,
                    now=now,
                ),
                database=database,
                now=now,
            ),
            checkpoint_store=checkpoint_store,
            calendar_timezone=calendar_timezone,
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
    required: bool = True,
    now: Callable[[], datetime],
) -> CheckpointedScheduledTask:
    return CheckpointedScheduledTask(
        task_id=task_id,
        required=required,
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


def _macro_release_task(
    *,
    task_id: str,
    provider_role: str,
    region: Region,
    executor: ScheduledTaskExecutor,
    checkpoint_store: ScheduledTaskCheckpointStore,
    calendar_timezone: ZoneInfo,
    now: Callable[[], datetime],
) -> CheckpointedScheduledTask:
    return CheckpointedScheduledTask(
        task_id=task_id,
        required=True,
        provider_role=provider_role,
        dataset=Dataset.MACRO_RELEASES,
        region=region,
        executor=executor,
        checkpoint_store=checkpoint_store,
        request_factory=_window_request_factory(
            provider_role=provider_role,
            dataset=Dataset.MACRO_RELEASES,
            region=region,
            calendar_timezone=calendar_timezone,
            start_offset=timedelta(),
            end_offset=timedelta(days=8),
        ),
        now=now,
    )


def _news_task(
    *,
    task_id: str,
    provider_role: str,
    region: Region,
    executor: ScheduledTaskExecutor,
    checkpoint_store: ScheduledTaskCheckpointStore,
    calendar_timezone: ZoneInfo,
    now: Callable[[], datetime],
) -> CheckpointedScheduledTask:
    return CheckpointedScheduledTask(
        task_id=task_id,
        required=True,
        provider_role=provider_role,
        dataset=Dataset.NEWS,
        region=region,
        executor=executor,
        checkpoint_store=checkpoint_store,
        request_factory=_window_request_factory(
            provider_role=provider_role,
            dataset=Dataset.NEWS,
            region=region,
            calendar_timezone=calendar_timezone,
            start_offset=timedelta(days=-1),
            end_offset=timedelta(days=1),
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
        end = datetime.combine(report_date, time_of_day.min, tzinfo=calendar_timezone).astimezone(
            UTC
        )
        return IngestJobRequest(
            provider_role=provider_role,
            dataset=Dataset.BARS,
            regions={region},
            start=end - timedelta(days=lookback_days),
            end=end,
            as_of=as_of,
            cursor=cursor,
        )

    return build


def _window_request_factory(
    *,
    provider_role: str,
    dataset: Dataset,
    region: Region,
    calendar_timezone: ZoneInfo,
    start_offset: timedelta,
    end_offset: timedelta,
) -> ScheduledRequestFactory:
    def build(report_date: date, as_of: datetime, cursor: str | None) -> IngestJobRequest:
        day_start = datetime.combine(
            report_date, time_of_day.min, tzinfo=calendar_timezone
        ).astimezone(UTC)
        return IngestJobRequest(
            provider_role=provider_role,
            dataset=dataset,
            regions={region},
            start=day_start + start_offset,
            end=day_start + end_offset,
            as_of=as_of,
            cursor=cursor,
        )

    return build


async def run_scheduler(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    provider_registry: ProviderRegistry | None = None,
    llm_client: LlmClient | None = None,
    http_client: httpx.AsyncClient | None = None,
    report_workflow: ScheduledReportWorkflow | None = None,
    report_version: str | None = None,
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
    owns_http_client = False
    resolved_http_client = http_client
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
                service="macro-data-worker",
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
        resolved_report_version = (
            resolved_settings.report_workflow_version if report_version is None else report_version
        )
        resolved_report_workflow = report_workflow
        if resolved_report_workflow is None and resolved_settings.feishu_delivery_enabled:
            if resolved_settings.feishu_alert_chat_id is None:
                raise SchedulerNotConfiguredError(
                    "FEISHU_ALERT_CHAT_ID is required for the complete daily workflow"
                )
            if resolved_settings.feishu_alert_chat_id == resolved_settings.feishu_chat_id:
                raise SchedulerNotConfiguredError(
                    "FEISHU_ALERT_CHAT_ID must differ from FEISHU_CHAT_ID"
                )
            if resolved_http_client is None:
                resolved_http_client = httpx.AsyncClient()
                owns_http_client = True
            generation_store = PostgresReportGenerationStore(resolved_database)
            resolved_report_workflow = DailyReportWorkflow(
                generation_service=build_generation_service(
                    llm=llm_client,
                    now=now,
                    timeout_seconds=resolved_settings.report_generation_timeout_seconds,
                    max_attempts=resolved_settings.report_generation_max_attempts,
                ),
                store=generation_store,
                report_delivery=ConfiguredFeishuDelivery(
                    settings=resolved_settings,
                    client=resolved_http_client,
                    store=PostgresReportDeliveryStore(resolved_database),
                ),
                alert_delivery=ConfiguredFeishuAlerts(
                    settings=resolved_settings,
                    client=resolved_http_client,
                    store=PostgresWorkflowAlertStore(resolved_database),
                ),
                model=resolved_settings.report_generation_model,
                report_version=resolved_report_version,
                timezone=schedule_timezone,
                publish_hour=resolved_settings.worker_report_publish_hour_local,
                publish_minute=resolved_settings.worker_report_publish_minute_local,
                now=now,
                sleeper=sleeper,
            )
        if resolved_settings.app_env == "production" and resolved_report_workflow is None:
            raise SchedulerNotConfiguredError(
                "production requires enabled Feishu report and alert delivery"
            )
        worker = ScheduledIngestionWorker(
            tasks=registered_tasks,
            report_date_lock=PostgresReportDateLock(resolved_database),
            max_attempts=resolved_settings.worker_max_task_attempts,
            retry_delay_seconds=resolved_settings.worker_retry_delay_seconds,
            sleeper=sleeper,
            report_workflow=resolved_report_workflow,
            input_materializer=ReportInputSnapshotMaterializer(
                evidence_store=PostgresReportInputEvidenceStore(
                    resolved_database,
                    market_max_age=timedelta(hours=resolved_settings.worker_market_freshness_hours),
                    news_max_age=timedelta(hours=resolved_settings.worker_news_freshness_hours),
                ),
                snapshot_store=PostgresReportInputSnapshotStore(resolved_database),
                now=now,
                cutoff_at=lambda report_date: _report_cutoff_at(
                    report_date=report_date,
                    timezone=schedule_timezone,
                    hour=resolved_settings.worker_report_cutoff_hour_local,
                    minute=resolved_settings.worker_report_cutoff_minute_local,
                ),
                sleeper=sleeper,
            ),
        )
        if resolved_backfill_start is not None and resolved_backfill_end is not None:
            await worker.backfill(resolved_backfill_start, resolved_backfill_end)
            return
        if resolved_run_once_date is not None:
            result = await worker.run_for_date(resolved_run_once_date)
            if result.status == "retryable":
                await worker.notify_retry_exhausted(result)
            return
        execution_time = _first_safe_run_time(
            schedule_hour=resolved_settings.worker_schedule_hour_local,
            schedule_minute=resolved_settings.worker_schedule_minute_local,
            cutoff_hour=resolved_settings.worker_report_cutoff_hour_local,
            cutoff_minute=resolved_settings.worker_report_cutoff_minute_local,
        )
        await _run_configured_schedule(
            worker,
            timezone=schedule_timezone,
            hour=execution_time.hour,
            minute=execution_time.minute,
            poll_seconds=resolved_settings.worker_schedule_poll_seconds,
            now=now,
            sleeper=sleeper,
            retry_delay_seconds=resolved_settings.worker_retry_delay_seconds,
            max_report_attempts=resolved_settings.worker_max_report_attempts,
            max_schedule_runs=max_schedule_runs,
        )
    finally:
        if owns_http_client and resolved_http_client is not None:
            await resolved_http_client.aclose()
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
    retry_delay_seconds: float,
    max_report_attempts: int,
    max_schedule_runs: int | None,
) -> None:
    if max_report_attempts < 1:
        raise ValueError("max_report_attempts must be at least one")
    completed_dates: set[date] = set()
    retry_attempts: dict[date, int] = {}
    next_attempt_at: dict[date, datetime] = {}
    schedule_runs = 0
    schedule_time = time_of_day(hour=hour, minute=minute)
    while max_schedule_runs is None or schedule_runs < max_schedule_runs:
        local_now = now().astimezone(timezone)
        if local_now.timetz().replace(tzinfo=None) >= schedule_time:
            report_date = local_now.date()
            due_at = next_attempt_at.get(report_date)
            if report_date not in completed_dates and (due_at is None or local_now >= due_at):
                result = await worker.run_for_date(report_date)
                schedule_runs += 1
                if result.status in {"succeeded", "degraded", "blocked"}:
                    completed_dates.add(report_date)
                    retry_attempts.pop(report_date, None)
                    next_attempt_at.pop(report_date, None)
                else:
                    attempt_no = retry_attempts.get(report_date, 0) + 1
                    retry_attempts[report_date] = attempt_no
                    if attempt_no >= max_report_attempts:
                        await worker.notify_retry_exhausted(result)
                        completed_dates.add(report_date)
                        next_attempt_at.pop(report_date, None)
                        continue
                    delay_seconds = (
                        poll_seconds
                        if result.status == "locked"
                        else retry_delay_seconds * (2 ** (attempt_no - 1))
                    )
                    next_attempt_at[report_date] = local_now + timedelta(seconds=delay_seconds)
                continue
        await sleeper(poll_seconds)


def _report_cutoff_at(*, report_date: date, timezone: ZoneInfo, hour: int, minute: int) -> datetime:
    return datetime.combine(report_date, time_of_day(hour=hour, minute=minute), tzinfo=timezone)


def _first_safe_run_time(
    *,
    schedule_hour: int,
    schedule_minute: int,
    cutoff_hour: int,
    cutoff_minute: int,
) -> time_of_day:
    """Return the collection start, which must leave time before the cutoff."""

    schedule_time = time_of_day(hour=schedule_hour, minute=schedule_minute)
    cutoff_time = time_of_day(hour=cutoff_hour, minute=cutoff_minute)
    if schedule_time >= cutoff_time:
        raise ValueError("worker schedule time must be before the report cutoff")
    return schedule_time
