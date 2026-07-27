"""Independent scheduled-ingestion worker primitives.

The executable deliberately starts with no registered provider tasks.  A
future issue must supply the report calendar and snapshot-materialization
mapping before a live provider may be scheduled; the worker itself remains
usable for manually injected tasks and date-scoped backfills.
"""

from __future__ import annotations

import asyncio
import hashlib
import signal
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Literal, Protocol

import structlog
from sqlalchemy import text

from macro_platform.config import get_settings
from macro_platform.observability import configure_logging
from macro_platform.observability.metrics import SCHEDULED_REPORT_RUNS, SCHEDULED_TASK_RUNS
from macro_platform.storage.database import Database

ScheduledTaskStatus = Literal["succeeded", "failed", "retryable"]
ScheduledWorkerStatus = Literal["succeeded", "degraded", "blocked", "retryable", "locked"]


@dataclass(frozen=True, slots=True)
class ScheduledTaskResult:
    task_id: str
    provider_role: str
    status: ScheduledTaskStatus
    attempt_no: int = 1
    run_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ScheduledWorkerResult:
    report_date: date
    status: ScheduledWorkerStatus
    task_results: tuple[ScheduledTaskResult, ...]


class ScheduledTask(Protocol):
    task_id: str
    required: bool

    async def run(self, report_date: date) -> ScheduledTaskResult: ...


class ReportDateLock(Protocol):
    def hold(self, report_date: date) -> AbstractAsyncContextManager[bool]: ...


class RetryableScheduledTaskError(RuntimeError):
    """A task failure that may be retried with bounded backoff."""


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
        self._logger = structlog.get_logger("scheduled_ingestion_worker")

    async def run_for_date(self, report_date: date) -> ScheduledWorkerResult:
        async with self._report_date_lock.hold(report_date) as acquired:
            if not acquired:
                result = ScheduledWorkerResult(report_date, "locked", ())
                SCHEDULED_REPORT_RUNS.labels(status=result.status).inc()
                await self._logger.ainfo(
                    "scheduled_report_locked",
                    report_date=report_date.isoformat(),
                    terminal=result.status,
                )
                return result

            task_results = tuple([await self._run_task(task, report_date) for task in self._tasks])
            status = _worker_status(task_results, self._tasks)
            result = ScheduledWorkerResult(report_date, status, task_results)
            SCHEDULED_REPORT_RUNS.labels(status=status).inc()
            await self._logger.ainfo(
                "scheduled_report_finished",
                report_date=report_date.isoformat(),
                terminal=status,
                task_count=len(task_results),
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
        for attempt_no in range(1, self._max_attempts + 1):
            try:
                task_result = await task.run(report_date)
                if task_result.task_id != task.task_id:
                    raise ValueError("scheduled task returned a mismatched task ID")
                result = replace(task_result, attempt_no=attempt_no)
            except RetryableScheduledTaskError as error:
                if attempt_no < self._max_attempts:
                    await self._logger.awarning(
                        "scheduled_task_retrying",
                        report_date=report_date.isoformat(),
                        task_id=task.task_id,
                        provider_role=provider_role,
                        attempt_no=attempt_no,
                        error_code=type(error).__name__,
                    )
                    await self._sleeper(self._retry_delay_seconds * (2 ** (attempt_no - 1)))
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
                report_date=report_date.isoformat(),
                task_id=result.task_id,
                provider_role=result.provider_role,
                attempt_no=result.attempt_no,
                run_id=result.run_id,
                terminal=result.status,
                error_code=result.error_code,
            )
            return result
        raise AssertionError("retry loop must return a task result")


def build_registered_tasks() -> tuple[ScheduledTask, ...]:
    """Return production tasks once a calendar-to-snapshot plan is implemented.

    #29 supplies the worker contract, locking and manual backfill seam only.
    Returning an empty tuple keeps an incomplete schedule from performing live
    ingestion or claiming that report-input materialization already exists.
    """

    return ()


async def run_scheduler() -> None:
    """Run the standalone worker process without inventing a production schedule."""

    logger = structlog.get_logger("scheduler")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stop.set)

    await logger.ainfo("scheduler_started", registered_jobs=len(build_registered_tasks()))
    await stop.wait()
    await logger.ainfo("scheduler_stopped")


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


def _report_date_lock_key(report_date: date) -> int:
    digest = hashlib.sha256(
        f"macro-data-platform:scheduled-report:{report_date.isoformat()}".encode()
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    asyncio.run(run_scheduler())


if __name__ == "__main__":
    main()
