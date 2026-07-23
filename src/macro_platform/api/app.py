from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError

from macro_platform.api.dependencies import require_service_token
from macro_platform.api.exception_handlers import (
    data_unavailable_handler,
    http_error_handler,
    provider_error_handler,
    validation_error_handler,
)
from macro_platform.api.routes import editor, health, instruments, macro, market, meta, news
from macro_platform.config import Settings, get_settings
from macro_platform.providers.base import ProviderError
from macro_platform.providers.registry import ProviderRegistry
from macro_platform.services.editor_context_service import DataUnavailableError
from macro_platform.storage.database import Database
from macro_platform.storage.repositories import DataRepository, EmptyDataRepository


def create_app(
    *,
    settings: Settings | None = None,
    repository: DataRepository | None = None,
    provider_registry: ProviderRegistry | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    resolved_repository = repository or EmptyDataRepository()
    resolved_registry = provider_registry or ProviderRegistry()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.database = Database(resolved_settings.database_url)
        app.state.repository = resolved_repository
        app.state.provider_registry = resolved_registry
        yield
        await resolved_registry.close()
        await app.state.database.dispose()

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
    return app


app = create_app()
