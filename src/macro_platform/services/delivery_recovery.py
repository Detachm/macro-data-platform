"""Protected, request-idempotent and audited manual report delivery recovery."""

from __future__ import annotations

from typing import Literal, Protocol
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from macro_platform.contracts.common import StrictModel
from macro_platform.services.report_delivery import (
    ManualDeliveryRetryError,
    ReportDeliveryResult,
)
from macro_platform.storage.database import Database
from macro_platform.storage.models import DeliveryOperatorActionRow
from macro_platform.storage.reporting import DeliveryOperatorAction
from macro_platform.storage.unit_of_work import UnitOfWork

DeliveryRecoveryStatus = Literal["pending", "succeeded", "rejected", "failed"]


class DeliveryRecoveryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DeliveryRecoveryResult(StrictModel):
    action_id: UUID
    request_id: UUID
    report_id: str
    delivery_id: UUID | None = None
    status: DeliveryRecoveryStatus
    delivery_status: str | None = None
    error_code: str | None = None
    replayed: bool = False


class DeliveryRecoveryPort(Protocol):
    async def retry(
        self,
        *,
        request_id: UUID,
        report_id: str,
        confirmed_not_delivered: bool,
    ) -> DeliveryRecoveryResult: ...


class DeliveryRetryPort(Protocol):
    async def inspect(self, *, report_id: str) -> ReportDeliveryResult: ...

    async def retry(
        self,
        *,
        report_id: str,
        confirmed_not_delivered: bool,
    ) -> ReportDeliveryResult: ...


class DeliveryRecoveryAuditStore(Protocol):
    async def reserve(self, action: DeliveryOperatorAction) -> bool: ...

    async def load_for_request(self, request_id: UUID) -> DeliveryOperatorAction | None: ...

    async def complete(
        self,
        *,
        action_id: UUID,
        status: Literal["succeeded", "rejected", "failed"],
        delivery_id: UUID | None,
        result_delivery_status: str | None,
        error_code: str | None,
    ) -> DeliveryOperatorAction: ...


class PostgresDeliveryRecoveryAuditStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def reserve(self, action: DeliveryOperatorAction) -> bool:
        async with UnitOfWork(self._database).transaction() as session:
            inserted = await session.execute(
                insert(DeliveryOperatorActionRow)
                .values(
                    action_id=action.action_id,
                    request_id=action.request_id,
                    report_id=action.report_id,
                    delivery_id=action.delivery_id,
                    action=action.action,
                    confirmed_not_delivered=action.confirmed_not_delivered,
                    prior_status=action.prior_status,
                    prior_attempt_no=action.prior_attempt_no,
                    status=action.status,
                    result_delivery_status=action.result_delivery_status,
                    error_code=action.error_code,
                )
                .on_conflict_do_nothing(constraint="uq_delivery_operator_action_request")
                .returning(DeliveryOperatorActionRow.action_id)
            )
            return inserted.scalar_one_or_none() is not None

    async def load_for_request(self, request_id: UUID) -> DeliveryOperatorAction | None:
        async with self._database.session() as session:
            row = await session.scalar(
                select(DeliveryOperatorActionRow).where(
                    DeliveryOperatorActionRow.request_id == request_id
                )
            )
        return _action_from_row(row) if row is not None else None

    async def complete(
        self,
        *,
        action_id: UUID,
        status: Literal["succeeded", "rejected", "failed"],
        delivery_id: UUID | None,
        result_delivery_status: str | None,
        error_code: str | None,
    ) -> DeliveryOperatorAction:
        async with UnitOfWork(self._database).transaction() as session:
            await session.execute(
                update(DeliveryOperatorActionRow)
                .where(
                    DeliveryOperatorActionRow.action_id == action_id,
                    DeliveryOperatorActionRow.status == "pending",
                )
                .values(
                    status=status,
                    delivery_id=delivery_id,
                    result_delivery_status=result_delivery_status,
                    error_code=error_code,
                )
            )
            row = await session.get(DeliveryOperatorActionRow, action_id)
            if row is None:
                raise RuntimeError("delivery operator action disappeared")
            return _action_from_row(row)


class DeliveryRecoveryService:
    def __init__(
        self,
        *,
        delivery: DeliveryRetryPort,
        audit_store: DeliveryRecoveryAuditStore,
    ) -> None:
        self._delivery = delivery
        self._audit_store = audit_store

    async def retry(
        self,
        *,
        request_id: UUID,
        report_id: str,
        confirmed_not_delivered: bool,
    ) -> DeliveryRecoveryResult:
        existing_action = await self._audit_store.load_for_request(request_id)
        if existing_action is not None:
            self._assert_same_request(
                existing_action,
                report_id=report_id,
                confirmed_not_delivered=confirmed_not_delivered,
            )
            reconciled = await self._reconcile_pending(existing_action)
            return _recovery_result(reconciled, replayed=True)

        try:
            inspection = await self._delivery.inspect(report_id=report_id)
        except ManualDeliveryRetryError as error:
            raise DeliveryRecoveryError(error.code, str(error)) from error
        attempt = inspection.delivery_attempt
        action = DeliveryOperatorAction(
            action_id=uuid4(),
            request_id=request_id,
            report_id=report_id,
            delivery_id=attempt.delivery_id if attempt is not None else None,
            confirmed_not_delivered=confirmed_not_delivered,
            prior_status=attempt.status if attempt is not None else "missing",
            prior_attempt_no=attempt.attempt_no if attempt is not None else None,
        )
        inserted = await self._audit_store.reserve(action)
        if not inserted:
            existing_action = await self._audit_store.load_for_request(request_id)
            if existing_action is None:
                raise RuntimeError("delivery recovery request conflict has no audit record")
            self._assert_same_request(
                existing_action,
                report_id=report_id,
                confirmed_not_delivered=confirmed_not_delivered,
            )
            reconciled = await self._reconcile_pending(existing_action)
            return _recovery_result(reconciled, replayed=True)
        if attempt is None:
            rejected = await self._audit_store.complete(
                action_id=action.action_id,
                status="rejected",
                delivery_id=None,
                result_delivery_status=None,
                error_code="DELIVERY_ATTEMPT_NOT_FOUND",
            )
            return _recovery_result(rejected)

        try:
            delivery = await self._delivery.retry(
                report_id=report_id,
                confirmed_not_delivered=confirmed_not_delivered,
            )
        except ManualDeliveryRetryError as error:
            rejected = await self._audit_store.complete(
                action_id=action.action_id,
                status="rejected",
                delivery_id=attempt.delivery_id,
                result_delivery_status=attempt.status,
                error_code=error.code,
            )
            return _recovery_result(rejected)
        except Exception as error:  # noqa: BLE001 - operator boundary returns a safe audit code
            failed = await self._audit_store.complete(
                action_id=action.action_id,
                status="failed",
                delivery_id=attempt.delivery_id,
                result_delivery_status=None,
                error_code=type(error).__name__[:64],
            )
            return _recovery_result(failed)

        recovered_attempt = delivery.delivery_attempt
        delivery_status = delivery.status or None
        succeeded = delivery_status == "succeeded"
        completed = await self._audit_store.complete(
            action_id=action.action_id,
            status="succeeded" if succeeded else "failed",
            delivery_id=(
                recovered_attempt.delivery_id
                if recovered_attempt is not None
                else attempt.delivery_id
            ),
            result_delivery_status=delivery_status,
            error_code=(
                None
                if succeeded
                else recovered_attempt.error_code
                if recovered_attempt is not None
                else "DELIVERY_RECOVERY_FAILED"
            ),
        )
        return _recovery_result(completed)

    async def _reconcile_pending(self, action: DeliveryOperatorAction) -> DeliveryOperatorAction:
        if action.status != "pending" or action.prior_attempt_no is None:
            return action
        try:
            inspection = await self._delivery.inspect(report_id=action.report_id)
        except ManualDeliveryRetryError:
            return action
        attempt = inspection.delivery_attempt
        if attempt is None or attempt.attempt_no <= action.prior_attempt_no:
            return action
        if attempt.status == "pending":
            return action
        succeeded = attempt.status == "succeeded"
        return await self._audit_store.complete(
            action_id=action.action_id,
            status="succeeded" if succeeded else "failed",
            delivery_id=attempt.delivery_id,
            result_delivery_status=attempt.status,
            error_code=None if succeeded else attempt.error_code or "DELIVERY_RECOVERY_FAILED",
        )

    @staticmethod
    def _assert_same_request(
        action: DeliveryOperatorAction,
        *,
        report_id: str,
        confirmed_not_delivered: bool,
    ) -> None:
        if (
            action.report_id != report_id
            or action.confirmed_not_delivered != confirmed_not_delivered
        ):
            raise DeliveryRecoveryError(
                "DELIVERY_RECOVERY_REQUEST_CONFLICT",
                "the request ID is already bound to different recovery parameters",
            )


def _action_from_row(row: DeliveryOperatorActionRow) -> DeliveryOperatorAction:
    return DeliveryOperatorAction(
        action_id=row.action_id,
        request_id=row.request_id,
        report_id=row.report_id,
        delivery_id=row.delivery_id,
        action=row.action,
        confirmed_not_delivered=row.confirmed_not_delivered,
        prior_status=row.prior_status,
        prior_attempt_no=row.prior_attempt_no,
        status=row.status,
        result_delivery_status=row.result_delivery_status,
        error_code=row.error_code,
    )


def _recovery_result(
    action: DeliveryOperatorAction,
    *,
    replayed: bool = False,
) -> DeliveryRecoveryResult:
    return DeliveryRecoveryResult(
        action_id=action.action_id,
        request_id=action.request_id,
        report_id=action.report_id,
        delivery_id=action.delivery_id,
        status=action.status,
        delivery_status=action.result_delivery_status,
        error_code=action.error_code,
        replayed=replayed,
    )


__all__ = [
    "DeliveryRecoveryAuditStore",
    "DeliveryRecoveryError",
    "DeliveryRecoveryPort",
    "DeliveryRecoveryResult",
    "DeliveryRecoveryService",
    "DeliveryRecoveryStatus",
    "DeliveryRetryPort",
    "PostgresDeliveryRecoveryAuditStore",
]
