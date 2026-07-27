from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from macro_platform.contracts.common import SourceRef, StrictModel
from macro_platform.contracts.provider import FetchContext
from macro_platform.normalization.common import canonical_json_checksum, stable_id, utc_now
from macro_platform.providers.base import (
    ProviderAuthenticationError,
    ProviderAuthorizationError,
    ProviderCursorError,
    ProviderError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

_MAX_CURSOR_OFFSET = 100_000


class LiveHttpProvider:
    """Shared transport boundary for allowlisted live adapters.

    The adapter owns an injected client only when it creates one itself. This makes
    the live providers deterministic under contract tests while keeping credentials
    and transport policy outside the parser.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None,
        base_url: str,
        allowed_hosts: frozenset[str],
        timeout_seconds: float,
        clock: Callable[[], datetime] = utc_now,
        cursor_signing_secret: str | None = None,
    ) -> None:
        validate_allowlisted_url(base_url, allowed_hosts)
        self._base_url = base_url
        self._clock = clock
        self._timeout_seconds = timeout_seconds
        self._cursor_signing_secret = (cursor_signing_secret or secrets.token_urlsafe(32)).encode(
            "utf-8"
        )
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _cursor_fingerprint(self, query: StrictModel, context: FetchContext) -> str:
        payload = query.model_dump(mode="json")
        payload.pop("cursor", None)
        payload["context_as_of"] = context.as_of.isoformat()
        return canonical_json_checksum(payload)

    def _encode_cursor(
        self,
        *,
        fingerprint: str,
        offset: int,
        snapshot_at: str,
        snapshot_watermark: str,
        last_record_key: str | None = None,
    ) -> str:
        if offset < 0 or offset > _MAX_CURSOR_OFFSET:
            raise ValueError("live provider cursor offset is outside the allowed range")
        body = json.dumps(
            {
                "version": 1,
                "fingerprint": fingerprint,
                "offset": offset,
                "snapshot_at": snapshot_at,
                "snapshot_watermark": snapshot_watermark,
                "last_record_key": last_record_key,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(self._cursor_signing_secret, body, hashlib.sha256).digest()
        return ".".join(
            (
                "live-v1",
                _b64encode(body),
                _b64encode(signature),
            )
        )

    def _decode_cursor(
        self, cursor: str | None, fingerprint: str
    ) -> tuple[int, str | None, str | None, str | None]:
        if cursor is None:
            return 0, None, None, None
        try:
            version, encoded_body, encoded_signature = cursor.split(".", maxsplit=2)
            body = base64.urlsafe_b64decode(_with_padding(encoded_body))
            signature = base64.urlsafe_b64decode(_with_padding(encoded_signature))
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
            raise ProviderCursorError(
                "live provider cursor is malformed", code="INVALID_CURSOR"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderCursorError(
                "live provider cursor is not an object", code="INVALID_CURSOR"
            )
        expected_signature = hmac.new(self._cursor_signing_secret, body, hashlib.sha256).digest()
        offset = payload.get("offset")
        last_record_key = payload.get("last_record_key")
        snapshot_at = payload.get("snapshot_at")
        snapshot_watermark = payload.get("snapshot_watermark")
        if (
            version != "live-v1"
            or not hmac.compare_digest(signature, expected_signature)
            or payload.get("version") != 1
            or payload.get("fingerprint") != fingerprint
            or not isinstance(offset, int)
            or offset < 0
            or offset > _MAX_CURSOR_OFFSET
            or last_record_key is not None
            and not isinstance(last_record_key, str)
            or not isinstance(snapshot_at, str)
            or not isinstance(snapshot_watermark, str)
        ):
            raise ProviderCursorError(
                "live provider cursor is not valid for this query", code="INVALID_CURSOR"
            )
        return offset, last_record_key, snapshot_at, snapshot_watermark

    async def _get_text(
        self,
        *,
        path: str = "",
        params: Mapping[str, str | int] | None = None,
        context_deadline: datetime,
    ) -> tuple[str, httpx.Response, datetime]:
        response = await self._get(
            path=path,
            params=params,
            context_deadline=context_deadline,
        )
        fetched_at = self._clock().astimezone(UTC)
        return response.text, response, fetched_at

    async def _get_json(
        self,
        *,
        path: str = "",
        params: Mapping[str, str | int] | None = None,
        context_deadline: datetime,
    ) -> tuple[dict[str, Any], httpx.Response, datetime]:
        response = await self._get(
            path=path,
            params=params,
            context_deadline=context_deadline,
        )
        fetched_at = self._clock().astimezone(UTC)
        content_type = response.headers.get("Content-Type", "").lower()
        body_prefix = response.text.lstrip().lower()[:256]
        if "text/html" in content_type or body_prefix.startswith(("<!doctype html", "<html")):
            raise ProviderAuthorizationError(
                "allowlisted provider returned an HTML challenge or login page",
                code="PROVIDER_FORBIDDEN",
            )
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderSchemaError(
                "allowlisted provider returned malformed JSON", code="MALFORMED_JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderSchemaError(
                "allowlisted provider returned a non-object JSON payload",
                code="SCHEMA_DRIFT",
            )
        return payload, response, fetched_at

    async def _get(
        self,
        *,
        path: str,
        params: Mapping[str, str | int] | None,
        context_deadline: datetime,
    ) -> httpx.Response:
        now = self._clock().astimezone(UTC)
        deadline = context_deadline.astimezone(UTC)
        remaining = (deadline - now).total_seconds()
        if remaining <= 0:
            raise ProviderTimeoutError("provider deadline has elapsed", retryable=True)
        timeout = min(self._timeout_seconds, remaining)
        url = f"{self._base_url.rstrip('/')}/{path.lstrip('/')}" if path else self._base_url
        try:
            response = await self._client.get(url, params=params, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                "allowlisted provider request timed out", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                "allowlisted provider request failed", retryable=True
            ) from exc

        if response.status_code in {401, 407}:
            raise ProviderAuthenticationError(
                "allowlisted provider authentication failed", code="PROVIDER_AUTHENTICATION_FAILED"
            )
        if response.status_code == 403:
            raise ProviderAuthorizationError(
                "allowlisted provider denied the request", code="PROVIDER_FORBIDDEN"
            )
        if response.status_code == 429:
            raise ProviderRateLimitError(
                "allowlisted provider rate limited the request",
                retryable=True,
                retry_after_seconds=_retry_after_seconds(response, now=now),
            )
        if 500 <= response.status_code <= 599:
            raise ProviderUnavailableError("allowlisted provider is unavailable", retryable=True)
        if response.status_code >= 400:
            raise ProviderError(
                f"allowlisted provider returned HTTP {response.status_code}",
                code="PROVIDER_HTTP_ERROR",
            )
        return response


def validate_allowlisted_url(url: str, allowed_hosts: frozenset[str]) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.hostname not in allowed_hosts
    ):
        raise ValueError("live provider URL is outside the approved HTTPS allowlist")


def source_ref(
    *,
    provider_id: str,
    provider_record_id: str,
    source_name: str,
    source_url: str,
    retrieved_at: datetime,
    checksum_payload: object,
) -> SourceRef:
    return SourceRef(
        provider_id=provider_id,
        provider_record_id=provider_record_id,
        source_name=source_name,
        source_url=source_url,
        retrieved_at=retrieved_at,
        checksum_sha256=canonical_json_checksum(checksum_payload),
    )


def stable_provider_record_id(namespace: str, *parts: object) -> str:
    return stable_id(namespace, *parts)


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value).astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            return None
        return max(0, int((retry_at - (now or utc_now())).total_seconds()))


def _retry_after_seconds(response: httpx.Response, *, now: datetime) -> int | None:
    return parse_retry_after(response.headers.get("Retry-After"), now=now)


def health_status_for_error(error: Exception) -> Literal["down", "not_configured"]:
    if isinstance(error, (ProviderAuthenticationError, ProviderAuthorizationError)):
        return "not_configured"
    return "down"


def assert_cursor_snapshot(expected: str | None, actual: str) -> None:
    """Fail closed when a continuation sees a different upstream snapshot."""

    if expected is not None and expected != actual:
        raise ProviderCursorError(
            "allowlisted provider snapshot changed during pagination",
            code="SNAPSHOT_CHANGED",
        )


def assert_cursor_snapshot_at(expected: str | None, actual: datetime) -> None:
    """Validate the cursor's snapshot timestamp against the current fetch.

    The source watermark is the equality check for a frozen upstream snapshot;
    this timestamp check prevents a continuation from carrying a snapshot
    timestamp from the future or from a malformed cursor.
    """

    if expected is None:
        return
    try:
        expected_at = datetime.fromisoformat(expected).astimezone(UTC)
    except ValueError as exc:
        raise ProviderCursorError(
            "allowlisted provider cursor snapshot timestamp is malformed",
            code="INVALID_CURSOR",
        ) from exc
    if expected_at > actual.astimezone(UTC):
        raise ProviderCursorError(
            "allowlisted provider cursor snapshot timestamp is from the future",
            code="SNAPSHOT_CHANGED",
        )


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _with_padding(value: str) -> str:
    return value + "=" * (-len(value) % 4)
