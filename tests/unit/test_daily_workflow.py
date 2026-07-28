from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from macro_platform.contracts.common import Region
from macro_platform.contracts.provider import Dataset
from macro_platform.contracts.report import ReportValidationIssue
from macro_platform.jobs.scheduled_types import ScheduledTaskResult, ScheduledWorkerResult
from macro_platform.services.daily_workflow import DailyReportWorkflow
from macro_platform.services.workflow_alerts import WorkflowAlert
from macro_platform.storage.reporting import ReportGenerationAttempt, StoredDailyReport

ROOT = Path(__file__).parents[2]
REPORT_DATE = date(2026, 7, 23)
NOW = datetime(2026, 7, 23, 0, 15, tzinfo=UTC)


def _report(*, publishable: bool = True, report_version: str = "v1") -> StoredDailyReport:
    payload = json.loads((ROOT / "tests/golden/daily_report_v1_success.json").read_text())
    validation_errors = []
    lifecycle_status = "validated"
    if not publishable:
        payload["status"] = "incomplete"
        payload["publication"]["decision"] = "not_published"
        payload["publication"]["reason_code"] = "VALIDATION_FAILED"
        payload["publication"]["published_at"] = None
        lifecycle_status = "failed"
        validation_errors = [
            ReportValidationIssue(
                code="FACT_VALUE_MISMATCH",
                message="configured validation failure",
            )
        ]
    return StoredDailyReport(
        report_id=f"daily-report-{REPORT_DATE.isoformat()}-{report_version}",
        report_date=REPORT_DATE,
        report_version=report_version,
        contract_version=payload["contract_version"],
        input_snapshot_id=payload["input_snapshot"]["snapshot_id"],
        status=payload["status"],
        publication_decision=payload["publication"]["decision"],
        generated_at=payload["generated_at"],
        payload={
            **payload,
            "report_id": f"daily-report-{REPORT_DATE.isoformat()}-{report_version}",
        },
        lifecycle_status=lifecycle_status,
        validation_errors=validation_errors,
    )


def _attempt(*, error_code: str | None = None) -> ReportGenerationAttempt:
    return ReportGenerationAttempt(
        generation_id=uuid4(),
        report_id=f"daily-report-{REPORT_DATE.isoformat()}-v1",
        report_version="v1",
        input_snapshot_id="snapshot-daily-2026-07-23-v1",
        lifecycle_status="failed" if error_code else "generated",
        prompt_version="daily-report-v1.0",
        model="test-model",
        input_fingerprint_sha256="a" * 64,
        error_code=error_code,
    )


def _worker_result(
    status: str = "succeeded",
    *,
    snapshot_id: str | None = "snapshot-daily-2026-07-23-v1",
) -> ScheduledWorkerResult:
    run_id = uuid4()
    return ScheduledWorkerResult(
        report_date=REPORT_DATE,
        status=status,  # type: ignore[arg-type]
        task_results=(
            ScheduledTaskResult(
                task_id="cn.daily-bars",
                provider_role="cn.bars.primary",
                status="succeeded",
                dataset=Dataset.BARS,
                region=Region.CN,
                run_id=run_id,
                run_ids=(run_id,),
            ),
        ),
        snapshot_id=snapshot_id,
        quality_status="passed" if status == "succeeded" else "blocked",
    )


class _Store:
    def __init__(self, report: StoredDailyReport | None = None) -> None:
        self.report = report

    async def load_report(self, report_id: str) -> StoredDailyReport | None:
        if self.report is not None and self.report.report_id == report_id:
            return self.report
        return None


class _Generator:
    def __init__(
        self,
        report: StoredDailyReport | None,
        *,
        error_code: str | None = None,
        exception: Exception | None = None,
    ) -> None:
        self.report = report
        self.attempt = _attempt(error_code=error_code)
        self.exception = exception
        self.calls: list[dict[str, object]] = []

    async def generate(self, _store: object, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.exception is not None:
            raise self.exception
        return SimpleNamespace(report=self.report, attempt=self.attempt)


class _ReportDelivery:
    def __init__(self, status: str = "succeeded", *, error_code: str | None = None) -> None:
        self.status = status
        self.error_code = error_code
        self.report_ids: list[str] = []

    async def deliver(self, *, report_id: str, dry_run: bool = False) -> object:
        assert dry_run is False
        self.report_ids.append(report_id)
        attempt = None if self.error_code is None else SimpleNamespace(error_code=self.error_code)
        return SimpleNamespace(status=self.status, delivery_attempt=attempt)


class _AlertDelivery:
    def __init__(self, status: str = "succeeded") -> None:
        self.status = status
        self.alerts: list[WorkflowAlert] = []

    async def deliver(self, alert: WorkflowAlert) -> object:
        self.alerts.append(alert)
        return SimpleNamespace(status=self.status)


def _workflow(
    *,
    generator: _Generator,
    store: _Store | None = None,
    report_delivery: _ReportDelivery | None = None,
    alert_delivery: _AlertDelivery | None = None,
    delays: list[float] | None = None,
    report_version: str = "v1",
) -> DailyReportWorkflow:
    async def sleep(delay: float) -> None:
        if delays is not None:
            delays.append(delay)

    return DailyReportWorkflow(
        generation_service=generator,  # type: ignore[arg-type]
        store=store or _Store(),  # type: ignore[arg-type]
        report_delivery=report_delivery or _ReportDelivery(),  # type: ignore[arg-type]
        alert_delivery=alert_delivery or _AlertDelivery(),  # type: ignore[arg-type]
        model="test-model",
        report_version=report_version,
        timezone=ZoneInfo("Asia/Shanghai"),
        publish_hour=8,
        publish_minute=30,
        now=lambda: NOW,
        sleeper=sleep,
    )


async def test_daily_workflow_generates_waits_and_delivers_once() -> None:
    report = _report()
    generator = _Generator(report)
    delivery = _ReportDelivery()
    alerts = _AlertDelivery()
    delays: list[float] = []

    result = await _workflow(
        generator=generator,
        report_delivery=delivery,
        alert_delivery=alerts,
        delays=delays,
    ).complete(_worker_result())

    assert result.status == "succeeded"
    assert result.terminal_stage == "completed"
    assert result.report_id == report.report_id
    assert result.delivery_status == "succeeded"
    assert delays == [15 * 60]
    assert delivery.report_ids == [report.report_id]
    assert alerts.alerts == []
    assert generator.calls[0]["snapshot_id"] == "snapshot-daily-2026-07-23-v1"


async def test_daily_workflow_marks_a_published_llm_fallback_degraded() -> None:
    generator = _Generator(_report(), error_code="LLM_UNAVAILABLE")

    result = await _workflow(generator=generator).complete(_worker_result())

    assert result.status == "degraded"
    assert result.delivery_status == "succeeded"
    assert result.error_code == "LLM_UNAVAILABLE"


async def test_daily_workflow_blocks_quality_failure_and_alerts_without_generation() -> None:
    generator = _Generator(_report())
    alerts = _AlertDelivery()

    result = await _workflow(generator=generator, alert_delivery=alerts).complete(
        _worker_result("blocked")
    )

    assert result.status == "blocked"
    assert result.terminal_stage == "quality_gate"
    assert result.alert_status == "succeeded"
    assert result.workflow_run_id is not None
    assert generator.calls == []
    assert len(alerts.alerts) == 1
    alert = alerts.alerts[0]
    assert alert.stage == "quality_gate"
    assert alert.provider_run_ids


async def test_daily_workflow_alerts_only_after_retry_budget_is_exhausted() -> None:
    generator = _Generator(_report())
    alerts = _AlertDelivery()
    workflow = _workflow(generator=generator, alert_delivery=alerts)
    retryable = _worker_result("retryable")

    pending = await workflow.complete(retryable)
    terminal = await workflow.notify_retry_exhausted(pending)

    assert pending.status == "retryable"
    assert pending.alert_status is None
    assert terminal.status == "blocked"
    assert terminal.error_code == "WORKFLOW_RETRY_EXHAUSTED"
    assert terminal.alert_status == "succeeded"
    assert len(alerts.alerts) == 1


async def test_daily_workflow_alerts_when_validation_is_not_publishable() -> None:
    delivery = _ReportDelivery()
    alerts = _AlertDelivery()

    result = await _workflow(
        generator=_Generator(_report(publishable=False)),
        report_delivery=delivery,
        alert_delivery=alerts,
    ).complete(_worker_result())

    assert result.status == "blocked"
    assert result.terminal_stage == "validation"
    assert result.error_code == "FACT_VALUE_MISMATCH"
    assert delivery.report_ids == []
    assert len(alerts.alerts) == 1


async def test_daily_workflow_alerts_on_uncertain_report_delivery() -> None:
    delivery = _ReportDelivery(
        "uncertain",
        error_code="FEISHU_SEND_OUTCOME_UNKNOWN",
    )
    alerts = _AlertDelivery()

    result = await _workflow(
        generator=_Generator(_report()),
        report_delivery=delivery,
        alert_delivery=alerts,
    ).complete(_worker_result())

    assert result.status == "blocked"
    assert result.terminal_stage == "delivery"
    assert result.delivery_status == "uncertain"
    assert result.error_code == "FEISHU_SEND_OUTCOME_UNKNOWN"
    assert len(alerts.alerts) == 1


async def test_daily_workflow_reuses_an_existing_validated_report_after_restart() -> None:
    report = _report()
    generator = _Generator(None, exception=AssertionError("must reuse persisted report"))
    delivery = _ReportDelivery()

    first = await _workflow(
        generator=generator,
        store=_Store(report),
        report_delivery=delivery,
    ).complete(_worker_result())
    second = await _workflow(
        generator=generator,
        store=_Store(report),
        report_delivery=delivery,
    ).complete(_worker_result())

    assert first.workflow_run_id == second.workflow_run_id
    assert generator.calls == []
    assert delivery.report_ids == [report.report_id, report.report_id]


async def test_daily_workflow_uses_an_explicit_version_for_safe_regeneration() -> None:
    first = await _workflow(generator=_Generator(_report())).complete(_worker_result())
    regenerated_report = _report(report_version="v2-reviewed")
    regenerated = await _workflow(
        generator=_Generator(regenerated_report),
        report_version="v2-reviewed",
    ).complete(_worker_result())

    assert first.workflow_run_id != regenerated.workflow_run_id
    assert regenerated.report_id == f"daily-report-{REPORT_DATE.isoformat()}-v2-reviewed"


async def test_daily_workflow_alerts_an_unhandled_scheduler_failure() -> None:
    alerts = _AlertDelivery()
    workflow = _workflow(generator=_Generator(_report()), alert_delivery=alerts)

    result = await workflow.notify_unhandled_failure(
        _worker_result(),
        error_code="UnexpectedRuntimeError",
    )

    assert result.status == "blocked"
    assert result.terminal_stage == "scheduler"
    assert result.alert_status == "succeeded"
    assert alerts.alerts[0].error_code == "UnexpectedRuntimeError"


def test_daily_workflow_rejects_an_unsafe_report_version() -> None:
    with pytest.raises(ValueError, match="invalid format"):
        _workflow(generator=_Generator(_report()), report_version="v2 unsafe")
