from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from macro_platform.services.delivery_recovery import (
    DeliveryRecoveryError,
    DeliveryRecoveryService,
)
from macro_platform.services.report_delivery import (
    ManualDeliveryRetryError,
    ReportDeliveryResult,
)
from macro_platform.storage.reporting import DeliveryAttempt, DeliveryOperatorAction

REPORT_ID = "daily-report-2026-07-28-v1"


def _attempt(
    *,
    status: str = "failed",
    attempt_no: int = 1,
    delivery_id: UUID | None = None,
) -> DeliveryAttempt:
    return DeliveryAttempt(
        delivery_id=delivery_id or uuid4(),
        report_id=REPORT_ID,
        report_version="v1",
        delivery_target="feishu:oc_test",
        idempotency_key="feishu:" + "a" * 64,
        attempt_no=attempt_no,
        status=status,  # type: ignore[arg-type]
        request_payload={"request_uuid": "b" * 50},
        error_code="FEISHU_SEND_FAILED" if status == "failed" else None,
    )


def _delivery_result(attempt: DeliveryAttempt | None) -> ReportDeliveryResult:
    return ReportDeliveryResult(
        report_id=REPORT_ID,
        report_version="v1",
        delivery_target="feishu:oc_test",
        idempotency_key="feishu:" + "a" * 64,
        card={},
        dry_run=False,
        delivery_attempt=attempt,
    )


class _Delivery:
    def __init__(
        self,
        attempt: DeliveryAttempt | None,
        *,
        retry_error: ManualDeliveryRetryError | None = None,
    ) -> None:
        self.attempt = attempt
        self.retry_error = retry_error
        self.inspect_calls = 0
        self.retry_calls = 0

    async def inspect(self, *, report_id: str) -> ReportDeliveryResult:
        assert report_id == REPORT_ID
        self.inspect_calls += 1
        return _delivery_result(self.attempt)

    async def retry(
        self,
        *,
        report_id: str,
        confirmed_not_delivered: bool,
    ) -> ReportDeliveryResult:
        del confirmed_not_delivered
        assert report_id == REPORT_ID
        self.retry_calls += 1
        if self.retry_error is not None:
            raise self.retry_error
        assert self.attempt is not None
        self.attempt = self.attempt.model_copy(
            update={
                "attempt_no": self.attempt.attempt_no + 1,
                "status": "succeeded",
                "message_id": "om_recovered",
                "error_code": None,
            }
        )
        return _delivery_result(self.attempt)


class _AuditStore:
    def __init__(self) -> None:
        self.actions: dict[UUID, DeliveryOperatorAction] = {}

    async def reserve(self, action: DeliveryOperatorAction) -> bool:
        if action.request_id in self.actions:
            return False
        self.actions[action.request_id] = action
        return True

    async def load_for_request(self, request_id: UUID) -> DeliveryOperatorAction | None:
        return self.actions.get(request_id)

    async def complete(
        self,
        *,
        action_id: UUID,
        status: str,
        delivery_id: UUID | None,
        result_delivery_status: str | None,
        error_code: str | None,
    ) -> DeliveryOperatorAction:
        request_id, current = next(
            (request_id, action)
            for request_id, action in self.actions.items()
            if action.action_id == action_id
        )
        if current.status == "pending":
            current = current.model_copy(
                update={
                    "status": status,
                    "delivery_id": delivery_id,
                    "result_delivery_status": result_delivery_status,
                    "error_code": error_code,
                }
            )
            self.actions[request_id] = current
        return current


async def test_delivery_recovery_is_idempotent_by_request_id() -> None:
    delivery = _Delivery(_attempt())
    audit = _AuditStore()
    service = DeliveryRecoveryService(delivery=delivery, audit_store=audit)  # type: ignore[arg-type]
    request_id = uuid4()

    first = await service.retry(
        request_id=request_id,
        report_id=REPORT_ID,
        confirmed_not_delivered=False,
    )
    replay = await service.retry(
        request_id=request_id,
        report_id=REPORT_ID,
        confirmed_not_delivered=False,
    )

    assert first.status == replay.status == "succeeded"
    assert first.action_id == replay.action_id
    assert first.replayed is False
    assert replay.replayed is True
    assert delivery.inspect_calls == 1
    assert delivery.retry_calls == 1
    assert len(audit.actions) == 1


async def test_delivery_recovery_audits_a_rejected_uncertain_retry() -> None:
    uncertain = _attempt(status="uncertain")
    delivery = _Delivery(
        uncertain,
        retry_error=ManualDeliveryRetryError(
            "DELIVERY_ABSENCE_CONFIRMATION_REQUIRED",
            "confirmation required",
        ),
    )
    audit = _AuditStore()
    service = DeliveryRecoveryService(delivery=delivery, audit_store=audit)  # type: ignore[arg-type]

    result = await service.retry(
        request_id=uuid4(),
        report_id=REPORT_ID,
        confirmed_not_delivered=False,
    )

    assert result.status == "rejected"
    assert result.error_code == "DELIVERY_ABSENCE_CONFIRMATION_REQUIRED"
    assert next(iter(audit.actions.values())).status == "rejected"


async def test_delivery_recovery_rejects_request_id_parameter_reuse() -> None:
    delivery = _Delivery(_attempt())
    audit = _AuditStore()
    service = DeliveryRecoveryService(delivery=delivery, audit_store=audit)  # type: ignore[arg-type]
    request_id = uuid4()
    await service.retry(
        request_id=request_id,
        report_id=REPORT_ID,
        confirmed_not_delivered=False,
    )

    with pytest.raises(DeliveryRecoveryError) as caught:
        await service.retry(
            request_id=request_id,
            report_id=REPORT_ID,
            confirmed_not_delivered=True,
        )

    assert caught.value.code == "DELIVERY_RECOVERY_REQUEST_CONFLICT"
    assert delivery.retry_calls == 1


async def test_delivery_recovery_reconciles_a_crash_after_the_send() -> None:
    delivery_id = uuid4()
    request_id = uuid4()
    action = DeliveryOperatorAction(
        action_id=uuid4(),
        request_id=request_id,
        report_id=REPORT_ID,
        delivery_id=delivery_id,
        prior_status="failed",
        prior_attempt_no=1,
    )
    audit = _AuditStore()
    audit.actions[request_id] = action
    delivery = _Delivery(_attempt(status="succeeded", attempt_no=2, delivery_id=delivery_id))
    service = DeliveryRecoveryService(delivery=delivery, audit_store=audit)  # type: ignore[arg-type]

    result = await service.retry(
        request_id=request_id,
        report_id=REPORT_ID,
        confirmed_not_delivered=False,
    )

    assert result.status == "succeeded"
    assert result.replayed is True
    assert delivery.retry_calls == 0
    assert audit.actions[request_id].result_delivery_status == "succeeded"
