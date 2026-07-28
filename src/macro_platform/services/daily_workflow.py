"""One date-scoped path from a persisted input snapshot to Feishu delivery."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, time
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from macro_platform.jobs.scheduled_types import ScheduledWorkerResult, ScheduledWorkerStatus
from macro_platform.services.llm import LlmClient, LlmError, LlmRequest, LlmResponse
from macro_platform.services.report_delivery import ReportDeliveryResult
from macro_platform.services.report_generator import (
    ReportGenerationService,
    ReportGenerationStore,
)
from macro_platform.services.workflow_alerts import (
    WorkflowAlert,
    WorkflowAlertDeliveryResult,
)
from macro_platform.storage.database import Database
from macro_platform.storage.reporting import (
    ReportGenerationAttempt,
    ReportInputSnapshot,
    StoredDailyReport,
)
from macro_platform.storage.repositories import ReportRepository
from macro_platform.storage.unit_of_work import UnitOfWork

_REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")
_REPORT_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


class DailyWorkflowStore(ReportGenerationStore, Protocol):
    async def load_report(self, report_id: str) -> StoredDailyReport | None: ...

    async def load_generation_attempt(
        self, generation_id: UUID
    ) -> ReportGenerationAttempt | None: ...


class ReportDeliveryPort(Protocol):
    async def deliver(self, *, report_id: str, dry_run: bool = False) -> ReportDeliveryResult: ...


class WorkflowAlertDeliveryPort(Protocol):
    async def deliver(self, alert: WorkflowAlert) -> WorkflowAlertDeliveryResult: ...


class PostgresReportGenerationStore:
    """Give generation and validation explicit independent transaction boundaries."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def load_input_snapshot(self, snapshot_id: str) -> ReportInputSnapshot | None:
        async with self._database.session() as session:
            return await ReportRepository(session).load_input_snapshot(snapshot_id)

    async def load_report(self, report_id: str) -> StoredDailyReport | None:
        async with self._database.session() as session:
            return await ReportRepository(session).load_report(report_id)

    async def load_generation_attempt(
        self,
        generation_id: UUID,
    ) -> ReportGenerationAttempt | None:
        async with self._database.session() as session:
            return await ReportRepository(session).load_generation_attempt(generation_id)

    async def put_generation_attempt(self, attempt: ReportGenerationAttempt) -> bool:
        async with UnitOfWork(self._database).transaction() as session:
            return await ReportRepository(session).put_generation_attempt(attempt)

    async def update_generation_attempt(self, attempt: ReportGenerationAttempt) -> None:
        async with UnitOfWork(self._database).transaction() as session:
            await ReportRepository(session).update_generation_attempt(attempt)

    async def put_report(self, report: StoredDailyReport) -> bool:
        async with UnitOfWork(self._database).transaction() as session:
            return await ReportRepository(session).put_report(report)

    async def update_report_validation(
        self,
        report: StoredDailyReport,
        *,
        expected_lifecycle_status: str,
    ) -> bool:
        async with UnitOfWork(self._database).transaction() as session:
            return await ReportRepository(session).update_report_validation(
                report,
                expected_lifecycle_status=expected_lifecycle_status,
            )


class DeterministicFallbackLlm:
    """Select the audited deterministic fallback until a real LLM is injected."""

    async def generate(self, request: LlmRequest) -> LlmResponse:
        del request
        raise LlmError("LLM client is not configured; use deterministic fallback")


class DailyReportWorkflow:
    """Complete one worker result while the report-date advisory lock is held."""

    def __init__(
        self,
        *,
        generation_service: ReportGenerationService,
        store: DailyWorkflowStore,
        report_delivery: ReportDeliveryPort,
        alert_delivery: WorkflowAlertDeliveryPort,
        model: str,
        report_version: str,
        model_parameters: Mapping[str, Any] | None = None,
        timezone: ZoneInfo = _REPORT_TIMEZONE,
        publish_hour: int = 8,
        publish_minute: int = 30,
        now: Callable[[], datetime],
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        publish_time = time(publish_hour, publish_minute)
        if not model.strip():
            raise ValueError("report generation model must not be empty")
        if _REPORT_VERSION_PATTERN.fullmatch(report_version) is None:
            raise ValueError("report workflow version has an invalid format")
        self._generation_service = generation_service
        self._store = store
        self._report_delivery = report_delivery
        self._alert_delivery = alert_delivery
        self._model = model
        self._report_version = report_version
        self._model_parameters = dict(model_parameters or {})
        self._timezone = timezone
        self._publish_time = publish_time
        self._now = now
        self._sleeper = sleeper

    async def complete(self, result: ScheduledWorkerResult) -> ScheduledWorkerResult:
        workflow_run_id = _workflow_run_id(result.report_date, self._report_version)
        current = replace(result, workflow_run_id=workflow_run_id)
        if current.status == "locked":
            return current
        if current.status == "retryable":
            return current
        if current.status == "blocked":
            stage = "quality_gate" if current.snapshot_id is not None else "ingestion"
            return await self._alert_terminal(
                current,
                stage=stage,
                error_code=current.error_code or _worker_error_code(current),
            )
        if current.snapshot_id is None:
            return await self._alert_terminal(
                replace(current, status="blocked"),
                stage="quality_gate",
                error_code="REPORT_INPUT_SNAPSHOT_MISSING",
            )

        report_id = _report_id(current.report_date, self._report_version)
        generation_error_code: str | None = None
        try:
            report = await self._store.load_report(report_id)
            if report is None:
                generated = await self._generation_service.generate(
                    self._store,
                    snapshot_id=current.snapshot_id,
                    report_id=report_id,
                    report_version=self._report_version,
                    model=self._model,
                    parameters=self._model_parameters,
                )
                report = generated.report
                generation_error_code = generated.attempt.error_code
            elif report.generation_id is not None:
                persisted_attempt = await self._store.load_generation_attempt(report.generation_id)
                if persisted_attempt is not None:
                    generation_error_code = persisted_attempt.error_code
        except Exception as error:  # noqa: BLE001 - workflow boundary fails closed
            return await self._alert_terminal(
                replace(current, status="blocked", report_id=report_id),
                stage="generation",
                error_code=type(error).__name__,
            )

        if report is None:
            return await self._alert_terminal(
                replace(current, status="blocked", report_id=report_id),
                stage=(
                    "quality_gate"
                    if generation_error_code
                    in {"REPORT_INPUT_QUALITY_BLOCKED", "REPORT_INPUT_QUALITY_RETRYABLE"}
                    else "generation"
                ),
                error_code=generation_error_code or "REPORT_GENERATION_FAILED",
            )
        if report.lifecycle_status != "validated" or report.publication_decision != "published":
            validation_code = (
                report.validation_errors[0].code
                if report.validation_errors
                else generation_error_code or "REPORT_NOT_PUBLISHABLE"
            )
            return await self._alert_terminal(
                replace(current, status="blocked", report_id=report.report_id),
                stage="validation",
                error_code=validation_code,
            )

        await self._wait_until_publish(current.report_date)
        try:
            delivery = await self._report_delivery.deliver(report_id=report.report_id)
        except Exception as error:  # noqa: BLE001 - transport boundary fails closed
            return await self._alert_terminal(
                replace(current, status="blocked", report_id=report.report_id),
                stage="delivery",
                error_code=type(error).__name__,
            )
        if delivery.status != "succeeded":
            delivery_error = (
                delivery.delivery_attempt.error_code
                if delivery.delivery_attempt is not None
                and delivery.delivery_attempt.error_code is not None
                else f"REPORT_DELIVERY_{delivery.status.upper()}"
            )
            return await self._alert_terminal(
                replace(
                    current,
                    status="blocked",
                    report_id=report.report_id,
                    delivery_status=delivery.status,
                ),
                stage="delivery",
                error_code=delivery_error,
            )

        final_status: ScheduledWorkerStatus = (
            "degraded"
            if current.status == "degraded" or generation_error_code is not None
            else "succeeded"
        )
        return replace(
            current,
            status=final_status,
            report_id=report.report_id,
            delivery_status=delivery.status,
            terminal_stage="completed",
            error_code=generation_error_code,
        )

    async def notify_retry_exhausted(
        self,
        result: ScheduledWorkerResult,
    ) -> ScheduledWorkerResult:
        current = replace(
            result,
            status="blocked",
            workflow_run_id=result.workflow_run_id
            or _workflow_run_id(result.report_date, self._report_version),
        )
        return await self._alert_terminal(
            current,
            stage="quality_gate" if current.snapshot_id is not None else "ingestion",
            error_code="WORKFLOW_RETRY_EXHAUSTED",
        )

    async def notify_unhandled_failure(
        self,
        result: ScheduledWorkerResult,
        *,
        error_code: str,
    ) -> ScheduledWorkerResult:
        return await self._alert_terminal(
            replace(
                result,
                status="blocked",
                workflow_run_id=result.workflow_run_id
                or _workflow_run_id(result.report_date, self._report_version),
            ),
            stage="scheduler",
            error_code=error_code,
        )

    async def _wait_until_publish(self, report_date: date) -> None:
        publish_at = datetime.combine(
            report_date,
            self._publish_time,
            tzinfo=self._timezone,
        ).astimezone(UTC)
        now = self._now().astimezone(UTC)
        if now < publish_at:
            await self._sleeper((publish_at - now).total_seconds())

    async def _alert_terminal(
        self,
        result: ScheduledWorkerResult,
        *,
        stage: str,
        error_code: str,
    ) -> ScheduledWorkerResult:
        stable_error_code = _stable_error_code(error_code)
        workflow_run_id = result.workflow_run_id or _workflow_run_id(
            result.report_date, self._report_version
        )
        alert = WorkflowAlert(
            workflow_run_id=workflow_run_id,
            report_date=result.report_date,
            stage=stage,
            error_code=stable_error_code,
            summary=_summary(stage, stable_error_code),
            safe_retry=_safe_retry(
                report_date=result.report_date,
                report_version=self._report_version,
                stage=stage,
            ),
            provider_run_ids=_provider_run_ids(result),
        )
        try:
            delivered = await self._alert_delivery.deliver(alert)
            alert_status = delivered.status
        except Exception:  # noqa: BLE001 - retain the original terminal workflow error
            alert_status = "failed"
        return replace(
            result,
            status="blocked",
            workflow_run_id=workflow_run_id,
            alert_status=alert_status,
            terminal_stage=stage,
            error_code=stable_error_code,
        )


def build_generation_service(
    *,
    llm: LlmClient | None,
    now: Callable[[], datetime],
    timeout_seconds: float,
    max_attempts: int,
) -> ReportGenerationService:
    return ReportGenerationService(
        llm or DeterministicFallbackLlm(),
        clock=now,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
    )


def _workflow_run_id(report_date: date, report_version: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"macro-data-platform:daily-workflow:{report_version}:{report_date.isoformat()}",
    )


def _report_id(report_date: date, report_version: str) -> str:
    return f"daily-report-{report_date.isoformat()}-{report_version}"


def _safe_retry(*, report_date: date, report_version: str, stage: str) -> str:
    command = f"`python -m macro_platform.jobs.scheduler --report-date {report_date.isoformat()}`"
    if stage in {"generation", "validation", "delivery"}:
        next_version = f"{report_version}-retry1"
        return (
            "先核对日报群和投递审计，确认没有已送达消息；修复原因后使用新的审核版本执行 "
            f"`python -m macro_platform.jobs.scheduler --report-date "
            f"{report_date.isoformat()} --report-version {next_version}`。"
            "同一版本不要直接重发。"
        )
    return f"确认依赖恢复并核对审计记录后，执行 {command}。同一版本会幂等复放。"


def _provider_run_ids(result: ScheduledWorkerResult) -> tuple[UUID, ...]:
    values = [run_id for task in result.task_results for run_id in task.evidence_run_ids]
    return tuple(dict.fromkeys(values))


def _worker_error_code(result: ScheduledWorkerResult) -> str:
    task_errors = sorted(
        {task.error_code for task in result.task_results if task.error_code is not None}
    )
    return task_errors[0] if task_errors else "WORKFLOW_BLOCKED"


def _stable_error_code(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]", "_", value.strip())
    return (normalized or "WORKFLOW_ERROR")[:64]


def _summary(stage: str, error_code: str) -> str:
    descriptions = {
        "ingestion": "必需采集任务未成功完成，正常日报已阻断。",
        "quality_gate": "08:15 输入快照未满足质量门禁，正常日报已阻断。",
        "generation": "报告生成未安全完成，正常日报已阻断。",
        "validation": "报告未通过事实与引用校验，正常日报已阻断。",
        "delivery": "正常日报未能确认送达日报群。",
        "scheduler": "定时 worker 未能安全完成本报告日期。",
    }
    return f"{descriptions.get(stage, descriptions['scheduler'])}（{error_code}）"


__all__ = [
    "DailyReportWorkflow",
    "DailyWorkflowStore",
    "DeterministicFallbackLlm",
    "PostgresReportGenerationStore",
    "ReportDeliveryPort",
    "WorkflowAlertDeliveryPort",
    "build_generation_service",
]
