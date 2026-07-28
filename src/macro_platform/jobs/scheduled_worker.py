"""Exclusive report-date orchestration, retry behavior, and observability."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import replace
from datetime import date
from typing import Literal, Protocol
from uuid import UUID

import structlog
from sqlalchemy import text

from macro_platform.jobs.scheduled_types import (
    ReportInputMaterializer,
    ScheduledReportWorkflow,
    ScheduledTask,
    ScheduledTaskResult,
    ScheduledWorkerResult,
    ScheduledWorkerStatus,
)
from macro_platform.observability.metrics import SCHEDULED_REPORT_RUNS, SCHEDULED_TASK_RUNS
from macro_platform.storage.database import Database

_WORKER_SERVICE = "macro-data-worker"


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
        report_workflow: ScheduledReportWorkflow | None = None,
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
        self._report_workflow = report_workflow
        self._logger = structlog.get_logger("scheduled_ingestion_worker")

    async def run_for_date(self, report_date: date) -> ScheduledWorkerResult:
        started = time.monotonic()
        if not self._tasks:
            result = ScheduledWorkerResult(report_date, "blocked", ())
            SCHEDULED_REPORT_RUNS.labels(status=result.status).inc()
            await self._log_report_finished(
                result,
                started=started,
                error_code="SCHEDULED_TASKS_NOT_CONFIGURED",
            )
            return result
        async with self._report_date_lock.hold(report_date) as acquired:
            if not acquired:
                result = ScheduledWorkerResult(report_date, "locked", ())
                SCHEDULED_REPORT_RUNS.labels(status=result.status).inc()
                await self._log_report_finished(
                    result,
                    started=started,
                    error_code="REPORT_DATE_LOCKED",
                )
                return result
            task_results = tuple([await self._run_task(task, report_date) for task in self._tasks])
            worker_status = _worker_status(task_results, self._tasks)
            snapshot_id: str | None = None
            quality_status: Literal["passed", "degraded", "blocked", "retryable"] | None = None
            materialization_error_code: str | None = None
            if self._input_materializer is not None:
                try:
                    materialized = await self._input_materializer.materialize(
                        report_date,
                        task_results=task_results,
                    )
                except Exception as error:  # noqa: BLE001 - quality materialization fails closed
                    worker_status = "blocked"
                    materialization_error_code = type(error).__name__
                else:
                    snapshot_id = materialized.snapshot.snapshot_id
                    quality_status = materialized.quality.status
                    worker_status = _combine_worker_and_quality_status(
                        worker_status,
                        quality_status,
                    )
            result = ScheduledWorkerResult(
                report_date=report_date,
                status=worker_status,
                task_results=task_results,
                snapshot_id=snapshot_id,
                quality_status=quality_status,
                error_code=materialization_error_code,
            )
            if self._report_workflow is not None:
                try:
                    result = await self._report_workflow.complete(result)
                except Exception as error:  # noqa: BLE001 - orchestration fails closed
                    failed = replace(
                        result,
                        status="blocked",
                        terminal_stage="scheduler",
                        error_code=type(error).__name__,
                    )
                    try:
                        result = await self._report_workflow.notify_unhandled_failure(
                            failed,
                            error_code=type(error).__name__,
                        )
                    except Exception:  # noqa: BLE001 - alert channel can also be unavailable
                        result = replace(failed, alert_status="failed")
            SCHEDULED_REPORT_RUNS.labels(status=result.status).inc()
            await self._log_report_finished(
                result,
                started=started,
                error_code=result.error_code,
            )
            return result

    async def _log_report_finished(
        self,
        result: ScheduledWorkerResult,
        *,
        started: float,
        error_code: str | None,
    ) -> None:
        run_ids = sorted(
            {str(task.run_id) for task in result.task_results if task.run_id is not None}
        )
        await self._logger.ainfo(
            "scheduled_report_finished",
            service=_WORKER_SERVICE,
            action="scheduled_report",
            run_id=run_ids[0] if len(run_ids) == 1 else None,
            provider_role=None,
            dataset=None,
            region=None,
            report_date=result.report_date.isoformat(),
            attempt_no=None,
            terminal=result.status,
            duration_ms=_duration_ms(started),
            record_count=sum(task.record_count for task in result.task_results),
            error_code=error_code,
            task_count=len(result.task_results),
            run_ids=run_ids,
            snapshot_id=result.snapshot_id,
            quality_status=result.quality_status,
            workflow_run_id=(
                str(result.workflow_run_id) if result.workflow_run_id is not None else None
            ),
            report_id=result.report_id,
            delivery_status=result.delivery_status,
            alert_status=result.alert_status,
            terminal_stage=result.terminal_stage,
        )

    async def notify_retry_exhausted(
        self,
        result: ScheduledWorkerResult,
    ) -> ScheduledWorkerResult:
        if self._report_workflow is None:
            return result
        return await self._report_workflow.notify_retry_exhausted(result)

    async def backfill(self, start_date: date, end_date: date) -> tuple[ScheduledWorkerResult, ...]:
        if end_date < start_date:
            raise ValueError("backfill end_date must not be before start_date")
        results: list[ScheduledWorkerResult] = []
        report_date = start_date
        while report_date <= end_date:
            result = await self.run_for_date(report_date)
            if result.status == "retryable":
                result = await self.notify_retry_exhausted(result)
            results.append(result)
            from datetime import timedelta

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
                        run_id=task_result.run_id,
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
                        run_id=None,
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
            except Exception as error:  # noqa: BLE001 - task boundary records terminal failure
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
                service=_WORKER_SERVICE,
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
        run_id: UUID | None,
        attempt_no: int,
        error_code: str | None,
    ) -> None:
        await self._logger.awarning(
            "scheduled_task_retrying",
            service=_WORKER_SERVICE,
            action="scheduled_task",
            run_id=str(run_id) if run_id is not None else None,
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


def _task_provider_role(task: ScheduledTask) -> str:
    value = getattr(task, "provider_role", task.task_id)
    return value if isinstance(value, str) and value else task.task_id


def _duration_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
