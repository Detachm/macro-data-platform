from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from macro_platform.contracts.common import ApiError, ErrorDetail, ErrorEnvelope
from macro_platform.providers.base import ProviderError
from macro_platform.services.editor_context_service import DataUnavailableError


def _request_id(request: Request) -> UUID:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, UUID) else uuid4()


def _response(request: Request, status_code: int, error: ApiError) -> JSONResponse:
    envelope = ErrorEnvelope(request_id=_request_id(request), error=error)
    return JSONResponse(status_code=status_code, content=envelope.model_dump(mode="json"))


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise TypeError("validation_error_handler received an unexpected exception")
    details = [
        ErrorDetail(
            location=[
                str(value) if not isinstance(value, int) else value for value in error["loc"]
            ],
            message=error["msg"],
            error_type=error["type"],
        )
        for error in exc.errors()
    ]
    return _response(
        request,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        ApiError(
            code="VALIDATION_ERROR",
            message="request validation failed",
            retryable=False,
            details=details,
        ),
    )


async def http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, HTTPException):
        raise TypeError("http_error_handler received an unexpected exception")
    detail: dict[str, object] = exc.detail if isinstance(exc.detail, dict) else {}
    code = str(detail.get("code", "HTTP_ERROR"))
    message = str(detail.get("message", exc.detail))
    return _response(
        request,
        exc.status_code,
        ApiError(code=code, message=message, retryable=False),
    )


async def data_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DataUnavailableError):
        raise TypeError("data_unavailable_handler received an unexpected exception")
    return _response(
        request,
        status.HTTP_503_SERVICE_UNAVAILABLE,
        ApiError(code="DATASET_UNAVAILABLE", message=str(exc), retryable=True),
    )


async def provider_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ProviderError):
        raise TypeError("provider_error_handler received an unexpected exception")
    status_code = (
        status.HTTP_429_TOO_MANY_REQUESTS
        if exc.code == "PROVIDER_RATE_LIMITED"
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return _response(
        request,
        status_code,
        ApiError(
            code=exc.code,
            message=str(exc),
            retryable=exc.retryable,
            retry_after_seconds=exc.retry_after_seconds,
        ),
    )
