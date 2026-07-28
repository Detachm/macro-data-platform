from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from macro_platform.config import Settings, get_settings
from macro_platform.providers.registry import ProviderRegistry
from macro_platform.services.delivery_recovery import DeliveryRecoveryPort
from macro_platform.services.workflow_operations import (
    WorkerReadinessReader,
    WorkflowOperationsReader,
)
from macro_platform.storage.database import Database
from macro_platform.storage.repositories import DataRepository

bearer_scheme = HTTPBearer(auto_error=False)


def request_id(request: Request) -> UUID:
    return cast(UUID, request.state.request_id)


def operator_request_id(request: Request) -> UUID:
    value = request.headers.get("X-Request-ID")
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "IDEMPOTENCY_REQUEST_ID_REQUIRED",
                "message": "a stable UUID X-Request-ID is required for this operation",
            },
        )
    try:
        return UUID(value)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "IDEMPOTENCY_REQUEST_ID_INVALID",
                "message": "X-Request-ID must be a UUID",
            },
        ) from error


def database(request: Request) -> Database:
    return cast(Database, request.app.state.database)


def repository(request: Request) -> DataRepository:
    return cast(DataRepository, request.app.state.repository)


def provider_registry(request: Request) -> ProviderRegistry:
    return cast(ProviderRegistry, request.app.state.provider_registry)


def workflow_operations(request: Request) -> WorkflowOperationsReader:
    return cast(WorkflowOperationsReader, request.app.state.workflow_operations)


def worker_readiness(request: Request) -> WorkerReadinessReader:
    return cast(WorkerReadinessReader, request.app.state.worker_readiness)


def delivery_recovery(request: Request) -> DeliveryRecoveryPort:
    value = getattr(request.app.state, "delivery_recovery", None)
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "DELIVERY_RECOVERY_NOT_CONFIGURED",
                "message": "protected Feishu delivery recovery is not configured",
            },
        )
    return cast(DeliveryRecoveryPort, value)


async def require_service_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    expected = settings.service_token.get_secret_value()
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHENTICATED", "message": "bearer token is required"},
        )
    if credentials.credentials != expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "invalid service token"},
        )


DatabaseDep = Annotated[Database, Depends(database)]
RepositoryDep = Annotated[DataRepository, Depends(repository)]
RegistryDep = Annotated[ProviderRegistry, Depends(provider_registry)]
WorkflowOperationsDep = Annotated[WorkflowOperationsReader, Depends(workflow_operations)]
WorkerReadinessDep = Annotated[WorkerReadinessReader, Depends(worker_readiness)]
DeliveryRecoveryDep = Annotated[DeliveryRecoveryPort, Depends(delivery_recovery)]
RequestIdDep = Annotated[UUID, Depends(request_id)]
OperatorRequestIdDep = Annotated[UUID, Depends(operator_request_id)]
