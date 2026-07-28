from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Response, status

from macro_platform.api.dependencies import (
    DeliveryRecoveryDep,
    OperatorRequestIdDep,
    RequestIdDep,
    WorkerReadinessDep,
    WorkflowOperationsDep,
)
from macro_platform.contracts.common import StrictModel, SuccessEnvelope
from macro_platform.normalization.common import utc_now
from macro_platform.services.delivery_recovery import (
    DeliveryRecoveryError,
    DeliveryRecoveryResult,
)
from macro_platform.services.workflow_operations import (
    DailyWorkflowOperationsStatus,
    WorkerReadinessStatus,
)

router = APIRouter(prefix="/v1/operations", tags=["operations"])


class DeliveryRetryRequest(StrictModel):
    confirmed_not_delivered: bool = False


@router.get(
    "/daily-workflows/{report_date}",
    response_model=SuccessEnvelope[DailyWorkflowOperationsStatus],
)
async def daily_workflow_status(
    report_date: date,
    operations: WorkflowOperationsDep,
    request_id: RequestIdDep,
) -> SuccessEnvelope[DailyWorkflowOperationsStatus]:
    now = utc_now()
    result = await operations.load(report_date)
    return SuccessEnvelope[DailyWorkflowOperationsStatus](
        request_id=request_id,
        as_of=now,
        snapshot_at=result.last_updated_at or now,
        data=result,
    )


@router.get(
    "/worker-readiness",
    response_model=SuccessEnvelope[WorkerReadinessStatus],
)
async def worker_readiness(
    readiness: WorkerReadinessDep,
    request_id: RequestIdDep,
    response: Response,
) -> SuccessEnvelope[WorkerReadinessStatus]:
    now = utc_now()
    result = await readiness.check()
    if result.status != "ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return SuccessEnvelope[WorkerReadinessStatus](
        request_id=request_id,
        as_of=now,
        snapshot_at=now,
        data=result,
    )


@router.post(
    "/daily-reports/{report_id}/delivery-retry",
    response_model=SuccessEnvelope[DeliveryRecoveryResult],
)
async def retry_daily_report_delivery(
    report_id: str,
    command: DeliveryRetryRequest,
    recovery: DeliveryRecoveryDep,
    request_id: OperatorRequestIdDep,
    response: Response,
) -> SuccessEnvelope[DeliveryRecoveryResult]:
    try:
        result = await recovery.retry(
            request_id=request_id,
            report_id=report_id,
            confirmed_not_delivered=command.confirmed_not_delivered,
        )
    except DeliveryRecoveryError as error:
        status_code = (
            status.HTTP_404_NOT_FOUND
            if error.code == "REPORT_NOT_FOUND"
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(
            status_code=status_code,
            detail={"code": error.code, "message": str(error)},
        ) from error
    if result.status == "pending":
        response.status_code = status.HTTP_202_ACCEPTED
    elif result.status == "rejected":
        response.status_code = status.HTTP_409_CONFLICT
    elif result.status == "failed":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    now = utc_now()
    return SuccessEnvelope[DeliveryRecoveryResult](
        request_id=request_id,
        as_of=now,
        snapshot_at=now,
        data=result,
    )


__all__ = ["router"]
