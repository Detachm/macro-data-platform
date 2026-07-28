from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError

from macro_platform.api.dependencies import require_service_token
from macro_platform.api.exception_handlers import (
    data_unavailable_handler,
    http_error_handler,
    provider_error_handler,
    validation_error_handler,
)
from macro_platform.api.routes import (
    editor,
    health,
    instruments,
    macro,
    market,
    meta,
    news,
    operations,
)
from macro_platform.config import Settings, get_settings
from macro_platform.providers.base import ProviderError
from macro_platform.providers.factory import create_provider_registry
from macro_platform.providers.registry import ProviderRegistry
from macro_platform.services.delivery_recovery import (
    DeliveryRecoveryPort,
    DeliveryRecoveryService,
    PostgresDeliveryRecoveryAuditStore,
)
from macro_platform.services.editor_context_service import DataUnavailableError
from macro_platform.services.report_delivery import (
    ConfiguredFeishuDelivery,
    PostgresReportDeliveryStore,
)
from macro_platform.services.workflow_operations import (
    PostgresWorkerReadinessReader,
    PostgresWorkflowOperationsReader,
    WorkerReadinessReader,
    WorkflowOperationsReader,
)
from macro_platform.storage.database import Database
from macro_platform.storage.repositories import (
    DataRepository,
    EmptyDataRepository,
    PostgresDataRepository,
)


def create_app(
    *,
    settings: Settings | None = None,
    repository: DataRepository | None = None,
    provider_registry: ProviderRegistry | None = None,
    workflow_operations: WorkflowOperationsReader | None = None,
    worker_readiness: WorkerReadinessReader | None = None,
    delivery_recovery: DeliveryRecoveryPort | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    resolved_database = Database(resolved_settings.database_url)
    resolved_repository = repository or (
        PostgresDataRepository(resolved_database)
        if resolved_settings.app_env == "production"
        else EmptyDataRepository()
    )
    if (
        resolved_settings.app_env == "production"
        and type(resolved_repository) is EmptyDataRepository
    ):
        raise ValueError("production app requires a PostgreSQL data repository")
    if provider_registry is not None:
        resolved_registry = provider_registry
    elif resolved_settings.provider_mode == "live":
        resolved_registry = create_provider_registry(resolved_settings)
    else:
        # Fixture providers are never auto-loaded. Tests and local tools must
        # opt into them through create_provider_registry(...).
        resolved_registry = ProviderRegistry()
    if resolved_settings.app_env == "production":
        resolved_registry.assert_production_safe()
    resolved_workflow_operations = workflow_operations or PostgresWorkflowOperationsReader(
        resolved_database
    )
    resolved_worker_readiness = worker_readiness or PostgresWorkerReadinessReader(
        resolved_database, resolved_settings
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned_http_client: httpx.AsyncClient | None = None
        resolved_delivery_recovery = delivery_recovery
        if resolved_delivery_recovery is None and resolved_settings.feishu_delivery_enabled:
            owned_http_client = httpx.AsyncClient()
            resolved_delivery_recovery = DeliveryRecoveryService(
                delivery=ConfiguredFeishuDelivery(
                    settings=resolved_settings,
                    client=owned_http_client,
                    store=PostgresReportDeliveryStore(resolved_database),
                ),
                audit_store=PostgresDeliveryRecoveryAuditStore(resolved_database),
            )
        app.state.database = resolved_database
        app.state.repository = resolved_repository
        app.state.provider_registry = resolved_registry
        app.state.workflow_operations = resolved_workflow_operations
        app.state.worker_readiness = resolved_worker_readiness
        app.state.delivery_recovery = resolved_delivery_recovery
        try:
            yield
        finally:
            if owned_http_client is not None:
                await owned_http_client.aclose()
            await resolved_registry.close()
            await resolved_database.dispose()

    app = FastAPI(
        title="Macro Data Platform",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.dependency_overrides[get_settings] = lambda: resolved_settings

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        header = request.headers.get("X-Request-ID")
        try:
            value = UUID(header) if header else uuid4()
        except ValueError:
            value = uuid4()
        request.state.request_id = value
        response = await call_next(request)
        response.headers["X-Request-ID"] = str(value)
        return response

    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(HTTPException, http_error_handler)
    app.add_exception_handler(DataUnavailableError, data_unavailable_handler)
    app.add_exception_handler(ProviderError, provider_error_handler)

    app.include_router(health.router)
    protected_dependencies = [Depends(require_service_token)]
    app.include_router(meta.router, dependencies=protected_dependencies)
    app.include_router(instruments.router, dependencies=protected_dependencies)
    app.include_router(market.router, dependencies=protected_dependencies)
    app.include_router(macro.router, dependencies=protected_dependencies)
    app.include_router(news.router, dependencies=protected_dependencies)
    app.include_router(editor.router, dependencies=protected_dependencies)
    app.include_router(operations.router, dependencies=protected_dependencies)
    return app


app = create_app()
