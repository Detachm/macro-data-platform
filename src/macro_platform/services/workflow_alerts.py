"""Durable, idempotent Feishu alerts for terminal daily-workflow failures."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from macro_platform.config import Settings
from macro_platform.contracts.common import StrictModel
from macro_platform.services.report_delivery import (
    FeishuCardTransport,
    FeishuTransport,
    FeishuTransportError,
)
from macro_platform.storage.database import Database
from macro_platform.storage.models import WorkflowAlertAttemptRow
from macro_platform.storage.reporting import WorkflowAlertAttempt
from macro_platform.storage.unit_of_work import UnitOfWork

WorkflowStage = Literal[
    "scheduler",
    "ingestion",
    "quality_gate",
    "generation",
    "validation",
    "delivery",
]

_REDACTED_KEY_NAMES = frozenset(
    {
        "accesstoken",
        "appsecret",
        "authorization",
        "password",
        "secret",
        "tenantaccesstoken",
        "token",
    }
)


class WorkflowAlert(StrictModel):
    """Sanitized operator-facing facts for one terminal workflow condition."""

    workflow_run_id: UUID
    report_date: date
    stage: WorkflowStage
    error_code: str
    summary: str
    safe_retry: str
    provider_run_ids: tuple[UUID, ...] = ()


class WorkflowAlertStore(Protocol):
    async def reserve(self, attempt: WorkflowAlertAttempt) -> bool: ...

    async def update(
        self,
        *,
        alert_id: UUID,
        expected_attempt_no: int,
        status: Literal["failed", "retry_wait", "succeeded", "uncertain"],
        response_payload: dict[str, Any] | None,
        message_id: str | None = None,
        error_code: str | None = None,
    ) -> bool: ...

    async def retry(self, alert_id: UUID) -> bool: ...

    async def load(self, alert_id: UUID) -> WorkflowAlertAttempt | None: ...

    async def load_for_key(self, idempotency_key: str) -> WorkflowAlertAttempt | None: ...


class PostgresWorkflowAlertStore:
    """Commit alert reservation and every transport state change independently."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def reserve(self, attempt: WorkflowAlertAttempt) -> bool:
        async with UnitOfWork(self._database).transaction() as session:
            inserted = await session.execute(
                insert(WorkflowAlertAttemptRow)
                .values(
                    alert_id=attempt.alert_id,
                    workflow_run_id=attempt.workflow_run_id,
                    report_date=attempt.report_date,
                    stage=attempt.stage,
                    delivery_target=attempt.delivery_target,
                    idempotency_key=attempt.idempotency_key,
                    attempt_no=attempt.attempt_no,
                    status=attempt.status,
                    request_payload=attempt.request_payload,
                    response_payload=attempt.response_payload,
                    message_id=attempt.message_id,
                    error_code=attempt.error_code,
                )
                .on_conflict_do_nothing(constraint="uq_workflow_alert_idempotency")
                .returning(WorkflowAlertAttemptRow.alert_id)
            )
            if inserted.scalar_one_or_none() is not None:
                return True
        existing = await self.load_for_key(attempt.idempotency_key)
        if existing is None:
            raise RuntimeError("workflow alert idempotency conflict has no stored attempt")
        if existing.request_payload != attempt.request_payload:
            raise ValueError("workflow alert idempotency key was reused for a different request")
        return False

    async def update(
        self,
        *,
        alert_id: UUID,
        expected_attempt_no: int,
        status: Literal["failed", "retry_wait", "succeeded", "uncertain"],
        response_payload: dict[str, Any] | None,
        message_id: str | None = None,
        error_code: str | None = None,
    ) -> bool:
        async with UnitOfWork(self._database).transaction() as session:
            result = await session.execute(
                update(WorkflowAlertAttemptRow)
                .where(
                    WorkflowAlertAttemptRow.alert_id == alert_id,
                    WorkflowAlertAttemptRow.attempt_no == expected_attempt_no,
                    WorkflowAlertAttemptRow.status == "pending",
                )
                .values(
                    status=status,
                    response_payload=response_payload,
                    message_id=message_id,
                    error_code=error_code,
                )
                .returning(WorkflowAlertAttemptRow.alert_id)
            )
            return result.scalar_one_or_none() is not None

    async def retry(self, alert_id: UUID) -> bool:
        async with UnitOfWork(self._database).transaction() as session:
            result = await session.execute(
                update(WorkflowAlertAttemptRow)
                .where(
                    WorkflowAlertAttemptRow.alert_id == alert_id,
                    WorkflowAlertAttemptRow.status == "retry_wait",
                )
                .values(
                    status="pending",
                    response_payload=None,
                    message_id=None,
                    error_code=None,
                    attempt_no=WorkflowAlertAttemptRow.attempt_no + 1,
                )
                .returning(WorkflowAlertAttemptRow.alert_id)
            )
            return result.scalar_one_or_none() is not None

    async def load(self, alert_id: UUID) -> WorkflowAlertAttempt | None:
        async with self._database.session() as session:
            row = await session.get(WorkflowAlertAttemptRow, alert_id)
        return None if row is None else _attempt_from_row(row)

    async def load_for_key(self, idempotency_key: str) -> WorkflowAlertAttempt | None:
        async with self._database.session() as session:
            row = await session.scalar(
                select(WorkflowAlertAttemptRow).where(
                    WorkflowAlertAttemptRow.idempotency_key == idempotency_key
                )
            )
        return None if row is None else _attempt_from_row(row)


class FeishuWorkflowAlertRenderer:
    """Render only sanitized workflow metadata into a red operator card."""

    def render(self, alert: WorkflowAlert) -> dict[str, Any]:
        provider_runs = (
            "、".join(str(run_id) for run_id in alert.provider_run_ids)
            if alert.provider_run_ids
            else "无"
        )
        return {
            "schema": "2.0",
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"宏观日报运行预警 · {alert.report_date.isoformat()}",
                },
                "template": "red",
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            f"**阶段**：{alert.stage}\n"
                            f"**错误码**：{alert.error_code}\n"
                            f"**Workflow Run ID**：{alert.workflow_run_id}\n"
                            f"**Provider Run IDs**：{provider_runs}"
                        ),
                    },
                    {"tag": "hr"},
                    {"tag": "markdown", "content": f"**现象**\n{alert.summary}"},
                    {
                        "tag": "markdown",
                        "content": f"**安全处置**\n{alert.safe_retry}",
                    },
                    {
                        "tag": "markdown",
                        "content": "请先核对运行审计和目标群，再执行按日期重试。",
                    },
                ],
            },
        }


@dataclass(frozen=True)
class WorkflowAlertDeliveryResult:
    alert: WorkflowAlert
    delivery_target: str
    idempotency_key: str
    card: dict[str, Any]
    attempt: WorkflowAlertAttempt

    @property
    def status(self) -> str:
        return self.attempt.status


class WorkflowAlertDeliveryService:
    """Send one alert with permanent deduplication and safe bounded retry."""

    def __init__(
        self,
        transport: FeishuCardTransport,
        *,
        renderer: FeishuWorkflowAlertRenderer | None = None,
        max_attempts: int = 3,
        retry_delay_seconds: float = 0.25,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("workflow alert max_attempts must be positive")
        if retry_delay_seconds < 0:
            raise ValueError("workflow alert retry delay must not be negative")
        self._transport = transport
        self._renderer = renderer or FeishuWorkflowAlertRenderer()
        self._max_attempts = max_attempts
        self._retry_delay_seconds = retry_delay_seconds
        self._sleep = sleep

    async def deliver(
        self,
        store: WorkflowAlertStore,
        *,
        alert: WorkflowAlert,
        chat_id: str,
    ) -> WorkflowAlertDeliveryResult:
        if not chat_id.strip():
            raise ValueError("Feishu alert chat ID is required")
        delivery_target = f"feishu-alert:{chat_id}"
        idempotency_key = _idempotency_key(alert, delivery_target)
        request_uuid = hashlib.sha256(idempotency_key.encode()).hexdigest()[:50]
        card = self._renderer.render(alert)
        initial = WorkflowAlertAttempt(
            alert_id=uuid4(),
            workflow_run_id=alert.workflow_run_id,
            report_date=alert.report_date,
            stage=alert.stage,
            delivery_target=delivery_target,
            idempotency_key=idempotency_key,
            request_payload={
                "provider": "feishu",
                "kind": "workflow_alert",
                "workflow_run_id": str(alert.workflow_run_id),
                "report_date": alert.report_date.isoformat(),
                "stage": alert.stage,
                "workflow_error_code": alert.error_code,
                "delivery_target": delivery_target,
                "request_uuid": request_uuid,
                "card": card,
            },
        )
        if await store.reserve(initial):
            attempt = initial
        else:
            existing = await store.load_for_key(idempotency_key)
            if existing is None:
                raise RuntimeError("workflow alert conflict has no stored attempt")
            if existing.status != "retry_wait" or existing.attempt_no >= self._max_attempts:
                return WorkflowAlertDeliveryResult(
                    alert=alert,
                    delivery_target=delivery_target,
                    idempotency_key=idempotency_key,
                    card=card,
                    attempt=existing,
                )
            resumed = await self._resume(store, existing)
            if resumed is None:
                current = await store.load(existing.alert_id)
                if current is None:
                    raise RuntimeError("workflow alert disappeared during retry reservation")
                return WorkflowAlertDeliveryResult(
                    alert, delivery_target, idempotency_key, card, current
                )
            attempt = resumed

        while True:
            try:
                sent = await self._transport.send_card(
                    chat_id=chat_id,
                    card=card,
                    request_uuid=request_uuid,
                )
            except FeishuTransportError as error:
                status: Literal["failed", "retry_wait", "uncertain"]
                if error.outcome_unknown:
                    status = "uncertain"
                elif error.retryable and attempt.attempt_no < self._max_attempts:
                    status = "retry_wait"
                else:
                    status = "failed"
                payload = _failure_payload(error)
                if not await store.update(
                    alert_id=attempt.alert_id,
                    expected_attempt_no=attempt.attempt_no,
                    status=status,
                    response_payload=payload,
                    error_code=error.error_code,
                ):
                    current = await store.load(attempt.alert_id)
                    if current is None:
                        raise RuntimeError(
                            "workflow alert disappeared during failure update"
                        ) from error
                    return WorkflowAlertDeliveryResult(
                        alert, delivery_target, idempotency_key, card, current
                    )
                attempt = attempt.model_copy(
                    update={
                        "status": status,
                        "response_payload": payload,
                        "error_code": error.error_code,
                    }
                )
                if status != "retry_wait":
                    return WorkflowAlertDeliveryResult(
                        alert, delivery_target, idempotency_key, card, attempt
                    )
                await self._sleep(self._retry_delay_seconds * (2 ** (attempt.attempt_no - 1)))
                resumed = await self._resume(store, attempt)
                if resumed is None:
                    current = await store.load(attempt.alert_id)
                    if current is None:
                        raise RuntimeError(
                            "workflow alert disappeared during retry reservation"
                        ) from error
                    return WorkflowAlertDeliveryResult(
                        alert, delivery_target, idempotency_key, card, current
                    )
                attempt = resumed
            else:
                payload = {
                    "provider": "feishu",
                    "result": "succeeded",
                    "response": _redact(sent.response_payload),
                }
                if not await store.update(
                    alert_id=attempt.alert_id,
                    expected_attempt_no=attempt.attempt_no,
                    status="succeeded",
                    response_payload=payload,
                    message_id=sent.message_id,
                ):
                    current = await store.load(attempt.alert_id)
                    if current is None:
                        raise RuntimeError("workflow alert disappeared during success update")
                    return WorkflowAlertDeliveryResult(
                        alert, delivery_target, idempotency_key, card, current
                    )
                succeeded = attempt.model_copy(
                    update={
                        "status": "succeeded",
                        "response_payload": payload,
                        "message_id": sent.message_id,
                        "error_code": None,
                    }
                )
                return WorkflowAlertDeliveryResult(
                    alert, delivery_target, idempotency_key, card, succeeded
                )

    async def _resume(
        self,
        store: WorkflowAlertStore,
        attempt: WorkflowAlertAttempt,
    ) -> WorkflowAlertAttempt | None:
        if not await store.retry(attempt.alert_id):
            return None
        return attempt.model_copy(
            update={
                "attempt_no": attempt.attempt_no + 1,
                "status": "pending",
                "response_payload": None,
                "message_id": None,
                "error_code": None,
            }
        )


class ConfiguredFeishuAlerts:
    """Bind the dedicated warning chat and app credentials to alert delivery."""

    def __init__(
        self,
        *,
        settings: Settings,
        client: httpx.AsyncClient,
        store: WorkflowAlertStore,
    ) -> None:
        if not settings.feishu_delivery_enabled:
            raise ValueError("Feishu delivery is disabled")
        if (
            settings.feishu_app_id is None
            or settings.feishu_app_secret is None
            or settings.feishu_alert_chat_id is None
            or not settings.feishu_alert_chat_id.strip()
        ):
            raise ValueError("enabled Feishu alerts require app credentials and alert chat ID")
        self._store = store
        self._chat_id = settings.feishu_alert_chat_id
        self._service = WorkflowAlertDeliveryService(
            FeishuTransport(
                app_id=settings.feishu_app_id,
                app_secret=settings.feishu_app_secret,
                client=client,
                api_base_url=settings.feishu_api_base_url,
                timeout_seconds=settings.feishu_timeout_seconds,
            ),
            max_attempts=settings.feishu_delivery_max_attempts,
        )

    async def deliver(self, alert: WorkflowAlert) -> WorkflowAlertDeliveryResult:
        return await self._service.deliver(
            self._store,
            alert=alert,
            chat_id=self._chat_id,
        )


def _attempt_from_row(row: WorkflowAlertAttemptRow) -> WorkflowAlertAttempt:
    return WorkflowAlertAttempt(
        alert_id=row.alert_id,
        workflow_run_id=row.workflow_run_id,
        report_date=row.report_date,
        stage=row.stage,
        delivery_target=row.delivery_target,
        idempotency_key=row.idempotency_key,
        attempt_no=row.attempt_no,
        status=cast(Any, row.status),
        request_payload=row.request_payload,
        response_payload=row.response_payload,
        message_id=row.message_id,
        error_code=row.error_code,
    )


def _idempotency_key(alert: WorkflowAlert, delivery_target: str) -> str:
    scope = ":".join(
        (
            str(alert.workflow_run_id),
            alert.report_date.isoformat(),
            alert.stage,
            alert.error_code,
            delivery_target,
        )
    )
    return hashlib.sha256(scope.encode()).hexdigest()


def _failure_payload(error: FeishuTransportError) -> dict[str, Any]:
    return {
        "provider": "feishu",
        "result": "failed",
        "error_code": error.error_code,
        "response": _redact(error.response_payload),
    }


def _redact(value: Any, *, key: str | None = None) -> Any:
    normalized = (
        ""
        if key is None
        else "".join(character for character in key.lower() if character.isalnum())
    )
    if normalized in _REDACTED_KEY_NAMES:
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            item_key: _redact(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


__all__ = [
    "ConfiguredFeishuAlerts",
    "FeishuWorkflowAlertRenderer",
    "PostgresWorkflowAlertStore",
    "WorkflowAlert",
    "WorkflowAlertDeliveryResult",
    "WorkflowAlertDeliveryService",
    "WorkflowAlertStore",
    "WorkflowStage",
]
