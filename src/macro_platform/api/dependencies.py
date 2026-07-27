from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from macro_platform.config import Settings, get_settings
from macro_platform.governance.source_policy import SourcePolicy
from macro_platform.providers.registry import ProviderRegistry
from macro_platform.storage.database import Database
from macro_platform.storage.repositories import DataRepository

bearer_scheme = HTTPBearer(auto_error=False)


def request_id(request: Request) -> UUID:
    return cast(UUID, request.state.request_id)


def database(request: Request) -> Database:
    return cast(Database, request.app.state.database)


def repository(request: Request) -> DataRepository:
    return cast(DataRepository, request.app.state.repository)


def provider_registry(request: Request) -> ProviderRegistry:
    return cast(ProviderRegistry, request.app.state.provider_registry)


def source_policy(request: Request) -> SourcePolicy:
    return cast(SourcePolicy, request.app.state.source_policy)


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
SourcePolicyDep = Annotated[SourcePolicy, Depends(source_policy)]
RequestIdDep = Annotated[UUID, Depends(request_id)]
