"""Read-only, sanitized operations view for one daily workflow date."""

from __future__ import annotations

from datetime import date
from typing import Literal, Protocol
from uuid import UUID

from pydantic import AwareDatetime, Field
from sqlalchemy import select, text

from macro_platform.config import Settings
from macro_platform.contracts.common import StrictModel
from macro_platform.services.report_input_quality import ReportInputQualityGate
from macro_platform.storage.database import Database
from macro_platform.storage.models import (
    DailyReportRow,
    DeliveryAttemptRow,
    DeliveryOperatorActionRow,
    ReportGenerationAttemptRow,
    ReportInputSnapshotRow,
    ScheduledTaskCheckpointRow,
    WorkflowAlertAttemptRow,
)
from macro_platform.storage.reporting import ReportInputSnapshot

WorkflowOperationalStatus = Literal[
    "not_started",
    "ingesting",
    "quality_blocked",
    "generating",
    "awaiting_delivery",
    "delivered",
    "blocked_alerted",
    "attention_required",
]


class WorkflowTaskView(StrictModel):
    task_id: str
    provider_role: str
    dataset: str
    region: str
    status: str
    run_id: UUID | None = None
    evidence_run_ids: tuple[UUID, ...] = ()
    records_accepted: int = Field(ge=0)
    records_rejected: int = Field(ge=0)
    updated_at: AwareDatetime


class WorkflowSnapshotView(StrictModel):
    snapshot_id: str
    snapshot_version: str
    quality_status: Literal["passed", "degraded", "blocked", "retryable"]
    quality_issue_codes: tuple[str, ...] = ()
    as_of: AwareDatetime
    cutoff_at: AwareDatetime
    fact_count: int = Field(ge=0)
    created_at: AwareDatetime


class WorkflowGenerationView(StrictModel):
    generation_id: UUID
    report_id: str
    report_version: str
    input_snapshot_id: str
    lifecycle_status: str
    attempt_no: int = Field(ge=1)
    model: str
    error_code: str | None = None
    updated_at: AwareDatetime


class WorkflowReportView(StrictModel):
    report_id: str
    report_version: str
    input_snapshot_id: str
    lifecycle_status: str
    report_status: str
    publication_decision: str
    generated_at: AwareDatetime
    validation_error_codes: tuple[str, ...] = ()
    created_at: AwareDatetime


class WorkflowDeliveryView(StrictModel):
    delivery_id: UUID
    report_id: str
    report_version: str
    status: str
    attempt_no: int = Field(ge=1)
    error_code: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class WorkflowAlertView(StrictModel):
    alert_id: UUID
    workflow_run_id: UUID
    stage: str
    status: str
    attempt_no: int = Field(ge=1)
    error_code: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class WorkflowOperatorActionView(StrictModel):
    action_id: UUID
    request_id: UUID
    report_id: str
    delivery_id: UUID | None = None
    action: str
    status: str
    confirmed_not_delivered: bool
    prior_status: str
    prior_attempt_no: int | None = Field(default=None, ge=1)
    result_delivery_status: str | None = None
    error_code: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class DailyWorkflowOperationsStatus(StrictModel):
    report_date: date
    status: WorkflowOperationalStatus
    operator_attention_required: bool
    tasks: tuple[WorkflowTaskView, ...] = ()
    snapshots: tuple[WorkflowSnapshotView, ...] = ()
    generations: tuple[WorkflowGenerationView, ...] = ()
    reports: tuple[WorkflowReportView, ...] = ()
    deliveries: tuple[WorkflowDeliveryView, ...] = ()
    alerts: tuple[WorkflowAlertView, ...] = ()
    operator_actions: tuple[WorkflowOperatorActionView, ...] = ()
    last_updated_at: AwareDatetime | None = None


class WorkerReadinessStatus(StrictModel):
    status: Literal["ready", "not_ready"]
    database_ready: bool
    schema_ready: bool
    provider_mode_live: bool
    us_provider_mode_live: bool
    us_credentials_configured: bool
    feishu_delivery_enabled: bool
    feishu_credentials_configured: bool
    daily_chat_configured: bool
    alert_chat_configured: bool
    chats_are_distinct: bool
    unmet_requirements: tuple[str, ...] = ()


class WorkflowOperationsReader(Protocol):
    async def load(self, report_date: date) -> DailyWorkflowOperationsStatus: ...


class WorkerReadinessReader(Protocol):
    async def check(self) -> WorkerReadinessStatus: ...


class PostgresWorkerReadinessReader:
    """Check production composition and schema without calling external providers."""

    def __init__(self, database: Database, settings: Settings) -> None:
        self._database = database
        self._settings = settings

    async def check(self) -> WorkerReadinessStatus:
        database_ready = await self._database.ready()
        schema_ready = await self._schema_ready() if database_ready else False
        provider_mode_live = self._settings.provider_mode == "live"
        us_provider_mode_live = self._settings.us_provider_mode == "live"
        us_credentials_configured = bool(
            self._settings.twelve_data_api_key is not None
            and self._settings.twelve_data_api_key.get_secret_value()
            and self._settings.twelve_data_cursor_secret is not None
            and self._settings.twelve_data_cursor_secret.get_secret_value()
        )
        feishu_credentials_configured = bool(
            self._settings.feishu_app_id
            and self._settings.feishu_app_secret is not None
            and self._settings.feishu_app_secret.get_secret_value()
        )
        daily_chat_configured = bool(self._settings.feishu_chat_id)
        alert_chat_configured = bool(self._settings.feishu_alert_chat_id)
        chats_are_distinct = bool(
            daily_chat_configured
            and alert_chat_configured
            and self._settings.feishu_chat_id != self._settings.feishu_alert_chat_id
        )
        checks = {
            "DATABASE_UNAVAILABLE": database_ready,
            "WORKFLOW_SCHEMA_INCOMPLETE": schema_ready,
            "PROVIDER_MODE_NOT_LIVE": provider_mode_live,
            "US_PROVIDER_MODE_NOT_LIVE": us_provider_mode_live,
            "US_PROVIDER_CREDENTIALS_MISSING": us_credentials_configured,
            "FEISHU_DELIVERY_DISABLED": self._settings.feishu_delivery_enabled,
            "FEISHU_CREDENTIALS_MISSING": feishu_credentials_configured,
            "FEISHU_DAILY_CHAT_MISSING": daily_chat_configured,
            "FEISHU_ALERT_CHAT_MISSING": alert_chat_configured,
            "FEISHU_CHATS_NOT_DISTINCT": chats_are_distinct,
        }
        unmet = tuple(code for code, passed in checks.items() if not passed)
        return WorkerReadinessStatus(
            status="ready" if not unmet else "not_ready",
            database_ready=database_ready,
            schema_ready=schema_ready,
            provider_mode_live=provider_mode_live,
            us_provider_mode_live=us_provider_mode_live,
            us_credentials_configured=us_credentials_configured,
            feishu_delivery_enabled=self._settings.feishu_delivery_enabled,
            feishu_credentials_configured=feishu_credentials_configured,
            daily_chat_configured=daily_chat_configured,
            alert_chat_configured=alert_chat_configured,
            chats_are_distinct=chats_are_distinct,
            unmet_requirements=unmet,
        )

    async def _schema_ready(self) -> bool:
        try:
            async with self._database.engine.connect() as connection:
                result = await connection.scalar(
                    text(
                        """
                        SELECT
                            to_regclass('public.scheduled_task_checkpoints') IS NOT NULL
                            AND to_regclass('public.report_input_snapshots') IS NOT NULL
                            AND to_regclass('public.report_generation_attempts') IS NOT NULL
                            AND to_regclass('public.daily_reports') IS NOT NULL
                            AND to_regclass('public.delivery_attempts') IS NOT NULL
                            AND to_regclass('public.workflow_alert_attempts') IS NOT NULL
                            AND to_regclass('public.delivery_operator_actions') IS NOT NULL
                        """
                    )
                )
            return bool(result)
        except Exception:  # noqa: BLE001 - readiness must return a safe negative result
            return False


class PostgresWorkflowOperationsReader:
    """Query workflow metadata without returning facts, cards, chats, or provider payloads."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._quality_gate = ReportInputQualityGate()

    async def load(self, report_date: date) -> DailyWorkflowOperationsStatus:
        async with self._database.session() as session:
            task_rows = (
                await session.scalars(
                    select(ScheduledTaskCheckpointRow)
                    .where(ScheduledTaskCheckpointRow.report_date == report_date)
                    .order_by(ScheduledTaskCheckpointRow.task_id)
                )
            ).all()
            snapshot_rows = (
                await session.scalars(
                    select(ReportInputSnapshotRow)
                    .where(ReportInputSnapshotRow.report_date == report_date)
                    .order_by(ReportInputSnapshotRow.created_at, ReportInputSnapshotRow.snapshot_id)
                )
            ).all()
            generation_rows = (
                await session.scalars(
                    select(ReportGenerationAttemptRow)
                    .join(
                        ReportInputSnapshotRow,
                        ReportGenerationAttemptRow.input_snapshot_id
                        == ReportInputSnapshotRow.snapshot_id,
                    )
                    .where(ReportInputSnapshotRow.report_date == report_date)
                    .order_by(
                        ReportGenerationAttemptRow.created_at,
                        ReportGenerationAttemptRow.generation_id,
                    )
                )
            ).all()
            report_rows = (
                await session.scalars(
                    select(DailyReportRow)
                    .where(DailyReportRow.report_date == report_date)
                    .order_by(DailyReportRow.created_at, DailyReportRow.report_id)
                )
            ).all()
            delivery_rows = (
                await session.scalars(
                    select(DeliveryAttemptRow)
                    .join(DailyReportRow, DeliveryAttemptRow.report_id == DailyReportRow.report_id)
                    .where(DailyReportRow.report_date == report_date)
                    .order_by(DeliveryAttemptRow.created_at, DeliveryAttemptRow.delivery_id)
                )
            ).all()
            alert_rows = (
                await session.scalars(
                    select(WorkflowAlertAttemptRow)
                    .where(WorkflowAlertAttemptRow.report_date == report_date)
                    .order_by(
                        WorkflowAlertAttemptRow.created_at,
                        WorkflowAlertAttemptRow.alert_id,
                    )
                )
            ).all()
            operator_action_rows = (
                await session.scalars(
                    select(DeliveryOperatorActionRow)
                    .join(
                        DailyReportRow,
                        DeliveryOperatorActionRow.report_id == DailyReportRow.report_id,
                    )
                    .where(DailyReportRow.report_date == report_date)
                    .order_by(
                        DeliveryOperatorActionRow.created_at,
                        DeliveryOperatorActionRow.action_id,
                    )
                )
            ).all()

        tasks = tuple(
            WorkflowTaskView(
                task_id=row.task_id,
                provider_role=row.provider_role,
                dataset=row.dataset,
                region=row.region,
                status=row.status,
                run_id=row.run_id,
                evidence_run_ids=_uuid_tuple(row.run_ids),
                records_accepted=row.records_accepted,
                records_rejected=row.records_rejected,
                updated_at=row.updated_at,
            )
            for row in task_rows
        )
        snapshots = tuple(self._snapshot_view(row) for row in snapshot_rows)
        generations = tuple(
            WorkflowGenerationView(
                generation_id=row.generation_id,
                report_id=row.report_id,
                report_version=row.report_version,
                input_snapshot_id=row.input_snapshot_id,
                lifecycle_status=row.lifecycle_status,
                attempt_no=row.attempt_no,
                model=row.model,
                error_code=row.error_code,
                updated_at=row.updated_at,
            )
            for row in generation_rows
        )
        reports = tuple(
            WorkflowReportView(
                report_id=row.report_id,
                report_version=row.report_version,
                input_snapshot_id=row.input_snapshot_id,
                lifecycle_status=row.lifecycle_status,
                report_status=row.status,
                publication_decision=row.publication_decision,
                generated_at=row.generated_at,
                validation_error_codes=tuple(
                    str(item.get("code", "UNKNOWN"))
                    for item in row.validation_errors
                    if isinstance(item, dict)
                ),
                created_at=row.created_at,
            )
            for row in report_rows
        )
        deliveries = tuple(
            WorkflowDeliveryView(
                delivery_id=row.delivery_id,
                report_id=row.report_id,
                report_version=row.report_version,
                status=row.status,
                attempt_no=row.attempt_no,
                error_code=row.error_code,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in delivery_rows
        )
        alerts = tuple(
            WorkflowAlertView(
                alert_id=row.alert_id,
                workflow_run_id=row.workflow_run_id,
                stage=row.stage,
                status=row.status,
                attempt_no=row.attempt_no,
                error_code=row.error_code,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in alert_rows
        )
        operator_actions = tuple(
            WorkflowOperatorActionView(
                action_id=row.action_id,
                request_id=row.request_id,
                report_id=row.report_id,
                delivery_id=row.delivery_id,
                action=row.action,
                status=row.status,
                confirmed_not_delivered=row.confirmed_not_delivered,
                prior_status=row.prior_status,
                prior_attempt_no=row.prior_attempt_no,
                result_delivery_status=row.result_delivery_status,
                error_code=row.error_code,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in operator_action_rows
        )
        status = _operational_status(
            tasks=tasks,
            snapshots=snapshots,
            generations=generations,
            reports=reports,
            deliveries=deliveries,
            alerts=alerts,
        )
        timestamps = [
            *(item.updated_at for item in tasks),
            *(item.created_at for item in snapshots),
            *(item.updated_at for item in generations),
            *(item.created_at for item in reports),
            *(item.updated_at for item in deliveries),
            *(item.updated_at for item in alerts),
            *(item.updated_at for item in operator_actions),
        ]
        return DailyWorkflowOperationsStatus(
            report_date=report_date,
            status=status,
            operator_attention_required=status
            in {"quality_blocked", "blocked_alerted", "attention_required"},
            tasks=tasks,
            snapshots=snapshots,
            generations=generations,
            reports=reports,
            deliveries=deliveries,
            alerts=alerts,
            operator_actions=operator_actions,
            last_updated_at=max(timestamps) if timestamps else None,
        )

    def _snapshot_view(self, row: ReportInputSnapshotRow) -> WorkflowSnapshotView:
        quality = self._quality_gate.evaluate(
            ReportInputSnapshot(
                snapshot_id=row.snapshot_id,
                snapshot_version=row.snapshot_version,
                report_date=row.report_date,
                as_of=row.as_of,
                cutoff_at=row.cutoff_at,
                fingerprint_sha256=row.fingerprint_sha256,
                fact_ids=row.fact_ids,
                payload=row.payload,
            )
        )
        return WorkflowSnapshotView(
            snapshot_id=row.snapshot_id,
            snapshot_version=row.snapshot_version,
            quality_status=quality.status,
            quality_issue_codes=tuple(issue.code for issue in quality.issues),
            as_of=row.as_of,
            cutoff_at=row.cutoff_at,
            fact_count=len(row.fact_ids),
            created_at=row.created_at,
        )


def _uuid_tuple(values: list[str]) -> tuple[UUID, ...]:
    return tuple(UUID(value) for value in values)


def _operational_status(
    *,
    tasks: tuple[WorkflowTaskView, ...],
    snapshots: tuple[WorkflowSnapshotView, ...],
    generations: tuple[WorkflowGenerationView, ...],
    reports: tuple[WorkflowReportView, ...],
    deliveries: tuple[WorkflowDeliveryView, ...],
    alerts: tuple[WorkflowAlertView, ...],
) -> WorkflowOperationalStatus:
    terminal_events = [
        *((item.updated_at, "delivery", item.status) for item in deliveries),
        *((item.updated_at, "alert", item.status) for item in alerts),
    ]
    if terminal_events:
        _, event_kind, event_status = max(terminal_events, key=lambda item: item[0])
        if event_kind == "delivery" and event_status == "succeeded":
            return "delivered"
        if event_kind == "alert" and event_status == "succeeded":
            return "blocked_alerted"
        if event_status in {"failed", "uncertain"}:
            return "attention_required"
        if event_kind == "delivery" and event_status in {"pending", "retry_wait"}:
            return "awaiting_delivery"
    if reports and reports[-1].lifecycle_status == "failed":
        return "attention_required"
    if reports and reports[-1].lifecycle_status == "validated":
        return "awaiting_delivery"
    if generations and generations[-1].lifecycle_status == "failed":
        return "attention_required"
    if snapshots and snapshots[-1].quality_status in {"blocked", "retryable"}:
        return "quality_blocked"
    if generations or reports:
        return "generating"
    if snapshots:
        return "generating"
    if tasks:
        return "ingesting"
    return "not_started"


__all__ = [
    "DailyWorkflowOperationsStatus",
    "PostgresWorkflowOperationsReader",
    "PostgresWorkerReadinessReader",
    "WorkflowAlertView",
    "WorkflowDeliveryView",
    "WorkflowGenerationView",
    "WorkflowOperationsReader",
    "WorkflowOperatorActionView",
    "WorkflowOperationalStatus",
    "WorkflowReportView",
    "WorkflowSnapshotView",
    "WorkflowTaskView",
    "WorkerReadinessReader",
    "WorkerReadinessStatus",
]
