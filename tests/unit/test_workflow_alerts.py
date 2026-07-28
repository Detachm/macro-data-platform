from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from macro_platform.config import Settings
from macro_platform.services.report_delivery import FeishuSendResult, FeishuTransportError
from macro_platform.services.workflow_alerts import (
    ConfiguredFeishuAlerts,
    FeishuWorkflowAlertRenderer,
    WorkflowAlert,
    WorkflowAlertDeliveryService,
)
from macro_platform.storage.reporting import WorkflowAlertAttempt


def _alert() -> WorkflowAlert:
    return WorkflowAlert(
        workflow_run_id=UUID("62f86247-e556-56af-8c6e-b74b6f51767d"),
        report_date=date(2026, 7, 28),
        stage="quality_gate",
        error_code="MISSING_REQUIRED_INPUT",
        summary="港股核心行情未满足 08:15 截止质量门禁。",
        safe_retry="确认数据中心恢复后，按 2026-07-28 显式重跑；不要直接重发日报。",
        provider_run_ids=(UUID("44e28f28-f6c6-45eb-b4f7-f47ba69eb6b8"),),
    )


class _Store:
    def __init__(self) -> None:
        self.attempts: dict[UUID, WorkflowAlertAttempt] = {}
        self.keys: dict[str, UUID] = {}
        self.reserve_calls = 0

    async def reserve(self, attempt: WorkflowAlertAttempt) -> bool:
        self.reserve_calls += 1
        if attempt.idempotency_key in self.keys:
            existing = self.attempts[self.keys[attempt.idempotency_key]]
            if existing.request_payload != attempt.request_payload:
                raise ValueError("alert key reused with different request")
            return False
        self.attempts[attempt.alert_id] = attempt
        self.keys[attempt.idempotency_key] = attempt.alert_id
        return True

    async def update(
        self,
        *,
        alert_id: UUID,
        expected_attempt_no: int,
        status: str,
        response_payload: dict[str, Any] | None,
        message_id: str | None = None,
        error_code: str | None = None,
    ) -> bool:
        current = self.attempts.get(alert_id)
        if (
            current is None
            or current.attempt_no != expected_attempt_no
            or current.status != "pending"
        ):
            return False
        self.attempts[alert_id] = current.model_copy(
            update={
                "status": status,
                "response_payload": response_payload,
                "message_id": message_id,
                "error_code": error_code,
            }
        )
        return True

    async def retry(self, alert_id: UUID) -> bool:
        current = self.attempts.get(alert_id)
        if current is None or current.status != "retry_wait":
            return False
        self.attempts[alert_id] = current.model_copy(
            update={
                "attempt_no": current.attempt_no + 1,
                "status": "pending",
                "response_payload": None,
                "message_id": None,
                "error_code": None,
            }
        )
        return True

    async def load(self, alert_id: UUID) -> WorkflowAlertAttempt | None:
        return self.attempts.get(alert_id)

    async def load_for_key(self, idempotency_key: str) -> WorkflowAlertAttempt | None:
        alert_id = self.keys.get(idempotency_key)
        return self.attempts.get(alert_id) if alert_id is not None else None


class _Transport:
    def __init__(self, responses: list[FeishuSendResult | FeishuTransportError]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, Mapping[str, Any], str]] = []

    async def send_card(
        self,
        *,
        chat_id: str,
        card: Mapping[str, Any],
        request_uuid: str,
    ) -> FeishuSendResult:
        self.requests.append((chat_id, card, request_uuid))
        result = self.responses.pop(0)
        if isinstance(result, FeishuTransportError):
            raise result
        return result


def test_workflow_alert_renderer_contains_safe_operator_context() -> None:
    card = FeishuWorkflowAlertRenderer().render(_alert())
    serialized = json.dumps(card, ensure_ascii=False)

    assert card["header"]["template"] == "red"
    assert "MISSING_REQUIRED_INPUT" in serialized
    assert str(_alert().workflow_run_id) in serialized
    assert "按 2026-07-28 显式重跑" in serialized


async def test_workflow_alert_is_permanently_idempotent() -> None:
    store = _Store()
    transport = _Transport([FeishuSendResult(message_id="om_alert", response_payload={"code": 0})])
    service = WorkflowAlertDeliveryService(transport)

    first = await service.deliver(store, alert=_alert(), chat_id="oc_alert")
    replay = await service.deliver(store, alert=_alert(), chat_id="oc_alert")

    assert first.status == "succeeded"
    assert first.attempt.message_id == "om_alert"
    assert replay.attempt == first.attempt
    assert len(transport.requests) == 1
    assert len(store.attempts) == 1
    assert len(transport.requests[0][2]) == 50


async def test_workflow_alert_retries_only_known_safe_rate_limit() -> None:
    store = _Store()
    transport = _Transport(
        [
            FeishuTransportError("FEISHU_RATE_LIMITED", retryable=True),
            FeishuSendResult(message_id="om_after_retry", response_payload={"code": 0}),
        ]
    )
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    result = await WorkflowAlertDeliveryService(
        transport,
        max_attempts=2,
        retry_delay_seconds=0.1,
        sleep=record_sleep,
    ).deliver(store, alert=_alert(), chat_id="oc_alert")

    assert result.status == "succeeded"
    assert result.attempt.attempt_no == 2
    assert delays == [0.1]
    assert len(transport.requests) == 2
    assert transport.requests[0][2] == transport.requests[1][2]


async def test_workflow_alert_does_not_replay_an_ambiguous_send() -> None:
    store = _Store()
    transport = _Transport(
        [FeishuTransportError("FEISHU_SEND_OUTCOME_UNKNOWN", outcome_unknown=True)]
    )
    service = WorkflowAlertDeliveryService(transport)

    first = await service.deliver(store, alert=_alert(), chat_id="oc_alert")
    replay = await service.deliver(store, alert=_alert(), chat_id="oc_alert")

    assert first.status == "uncertain"
    assert replay.attempt == first.attempt
    assert len(transport.requests) == 1


async def test_workflow_alert_redacts_transport_secrets() -> None:
    store = _Store()
    transport = _Transport(
        [
            FeishuTransportError(
                "FEISHU_AUTH_FAILED",
                response_payload={"tenantAccessToken": "must-not-persist"},
            )
        ]
    )

    result = await WorkflowAlertDeliveryService(transport).deliver(
        store,
        alert=_alert(),
        chat_id="oc_alert",
    )

    assert result.status == "failed"
    assert result.attempt.response_payload == {
        "provider": "feishu",
        "result": "failed",
        "error_code": "FEISHU_AUTH_FAILED",
        "response": {"tenantAccessToken": "[REDACTED]"},
    }


async def test_configured_workflow_alert_uses_only_the_warning_chat() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_alert"}})

    settings = Settings(
        _env_file=None,
        feishu_delivery_enabled=True,
        feishu_app_id="cli_test",
        feishu_app_secret=SecretStr("secret-test"),
        feishu_chat_id="oc_daily",
        feishu_alert_chat_id="oc_alert",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ConfiguredFeishuAlerts(
            settings=settings,
            client=client,
            store=_Store(),
        ).deliver(_alert())

    assert result.status == "succeeded"
    message = json.loads(requests[-1].content)
    assert message["receive_id"] == "oc_alert"
    assert message["receive_id"] != settings.feishu_chat_id


async def test_configured_workflow_alert_requires_the_warning_chat() -> None:
    settings = Settings(
        _env_file=None,
        feishu_delivery_enabled=True,
        feishu_app_id="cli_test",
        feishu_app_secret=SecretStr("secret-test"),
        feishu_chat_id="oc_daily",
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="alert chat ID"):
            ConfiguredFeishuAlerts(settings=settings, client=client, store=_Store())


def test_report_cutoff_must_precede_publish_time() -> None:
    with pytest.raises(ValueError, match="WORKER_REPORT_CUTOFF"):
        Settings(
            _env_file=None,
            worker_report_cutoff_hour_local=8,
            worker_report_cutoff_minute_local=30,
            worker_report_publish_hour_local=8,
            worker_report_publish_minute_local=30,
        )
