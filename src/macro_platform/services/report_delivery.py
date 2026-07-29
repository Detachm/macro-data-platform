from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
from pydantic import SecretStr, ValidationError

from macro_platform.config import Settings
from macro_platform.contracts.report import DailyReport, ReportSourceReference
from macro_platform.storage.database import Database
from macro_platform.storage.reporting import DeliveryAttempt, StoredDailyReport
from macro_platform.storage.repositories import ReportRepository
from macro_platform.storage.unit_of_work import UnitOfWork

_REDACTED_KEY_NAMES = frozenset(
    {
        "accesstoken",
        "appsecret",
        "authorization",
        "password",
        "secret",
        "tenantaccesstoken",
        "token",
    }
)
_TOKEN_REFRESH_MARGIN = timedelta(seconds=60)
_REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")
_MAX_FEISHU_CARD_BYTES = 30 * 1024
_FEISHU_RATE_LIMIT_CODES = frozenset({230020, 11232, 11233, 99991400})
_FEISHU_AUTH_CODES = frozenset({230006, 230027})
_FEISHU_CARD_INVALID_CODES = frozenset({200621, 200732, 200861, 230099})
_FEISHU_CHAT_UNAVAILABLE_CODES = frozenset(
    {230002, 230013, 230018, 230034, 230035, 230053, 230054, 230075, 232009}
)


class ReportDeliveryError(RuntimeError):
    """The report cannot be safely prepared or delivered."""


class ManualDeliveryRetryError(ReportDeliveryError):
    """A protected manual retry was rejected before any network request."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FeishuTransportError(RuntimeError):
    """A classified Feishu transport failure.

    ``outcome_unknown`` is deliberately stronger than ``retryable``: after a
    message request has an ambiguous outcome, callers must not resend the
    card automatically because Feishu may already have accepted it. Audit
    callers must redact ``response_payload`` before persistence.
    """

    def __init__(
        self,
        error_code: str,
        *,
        response_payload: dict[str, Any] | None = None,
        retryable: bool = False,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.response_payload = response_payload
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True)
class FeishuSendResult:
    message_id: str
    response_payload: dict[str, Any]


class FeishuCardTransport(Protocol):
    async def send_card(
        self,
        *,
        chat_id: str,
        card: Mapping[str, Any],
        request_uuid: str,
    ) -> FeishuSendResult: ...


class FeishuCardRenderer:
    """Map only validated DailyReport text into the frozen Feishu-card shape."""

    def render(self, report: StoredDailyReport) -> dict[str, Any]:
        try:
            typed_report = DailyReport.model_validate(report.payload)
        except ValidationError as error:
            raise ReportDeliveryError(
                "report payload does not satisfy DailyReport contract"
            ) from error

        if typed_report.report_id != report.report_id:
            raise ReportDeliveryError("report payload identity does not match delivery record")
        if typed_report.publication.decision != "published":
            raise ReportDeliveryError("not-published reports cannot render a Feishu report card")

        summary = _summary_display_text(typed_report)
        cn_highlights = _section_display_text(typed_report, "cn_highlights")
        hk_highlights = _section_display_text(typed_report, "hk_highlights")
        us_highlights = _section_display_text(typed_report, "us_highlights")
        calendar = _calendar_display_text(typed_report)
        quality_notice = _section_display_text(typed_report, "data_quality_notice")
        sources = _source_display_text(typed_report)
        template = "blue" if typed_report.status == "complete" else "orange"
        return {
            "schema": "2.0",
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"宏观日报 · {typed_report.report_date.isoformat()}",
                },
                "template": template,
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": [
                    {"tag": "markdown", "content": f"**摘要**\n{summary}"},
                    {"tag": "hr"},
                    {"tag": "markdown", "content": f"**中国内地**\n{cn_highlights}"},
                    {"tag": "markdown", "content": f"**香港**\n{hk_highlights}"},
                    {"tag": "markdown", "content": f"**美国**\n{us_highlights}"},
                    {
                        "tag": "markdown",
                        "content": f"**未来日程**\n{calendar}",
                    },
                    {"tag": "markdown", "content": f"**数据质量**\n{quality_notice}"},
                    {"tag": "markdown", "content": f"**来源**\n{sources}"},
                    {
                        "tag": "markdown",
                        "content": (
                            f"DailyReport v{typed_report.contract_version} · 仅展示已验证事实"
                        ),
                    },
                ],
            },
        }


class FeishuTransport:
    """Small Feishu client with cached tenant token and redacted failures."""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: SecretStr | str,
        client: httpx.AsyncClient,
        api_base_url: str = "https://open.feishu.cn",
        timeout_seconds: float = 15,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not app_id.strip():
            raise ValueError("Feishu app ID is required")
        secret = app_secret.get_secret_value() if isinstance(app_secret, SecretStr) else app_secret
        if not secret.strip():
            raise ValueError("Feishu app secret is required")
        if not api_base_url.startswith("https://"):
            raise ValueError("Feishu API base URL must be an absolute HTTPS URL")
        if timeout_seconds <= 0:
            raise ValueError("Feishu timeout must be positive")
        self._app_id = app_id
        self._app_secret = secret
        self._client = client
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._clock = clock or _utc_now
        self._token_lock = asyncio.Lock()
        self._tenant_access_token: str | None = None
        self._token_expires_at: datetime | None = None

    async def send_card(
        self,
        *,
        chat_id: str,
        card: Mapping[str, Any],
        request_uuid: str,
    ) -> FeishuSendResult:
        if not chat_id.strip():
            raise ValueError("Feishu chat ID is required")
        if not request_uuid or len(request_uuid) > 50:
            raise ValueError("Feishu request UUID must contain 1 to 50 characters")
        content = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        if len(content.encode()) > _MAX_FEISHU_CARD_BYTES:
            raise FeishuTransportError(
                "FEISHU_CARD_INVALID",
                response_payload={"reason": "card content exceeds the 30 KB API limit"},
            )
        token = await self._tenant_token()
        return await self._send_card_with_token(
            chat_id=chat_id,
            content=content,
            request_uuid=request_uuid,
            token=token,
            allow_token_refresh=True,
        )

    async def _tenant_token(self) -> str:
        now = self._clock()
        if self._token_is_fresh(now):
            assert self._tenant_access_token is not None
            return self._tenant_access_token

        async with self._token_lock:
            now = self._clock()
            if self._token_is_fresh(now):
                assert self._tenant_access_token is not None
                return self._tenant_access_token
            payload = await self._request_token()
            token = payload.get("tenant_access_token")
            expires_in = payload.get("expire", payload.get("expires_in"))
            if not isinstance(token, str) or not token:
                raise FeishuTransportError(
                    "FEISHU_TOKEN_RESPONSE_INVALID",
                    response_payload=payload,
                )
            if not isinstance(expires_in, int) or expires_in <= 0:
                raise FeishuTransportError(
                    "FEISHU_TOKEN_EXPIRY_INVALID",
                    response_payload=payload,
                )
            self._tenant_access_token = token
            self._token_expires_at = now + timedelta(seconds=expires_in)
            return token

    def _token_is_fresh(self, now: datetime) -> bool:
        return (
            self._tenant_access_token is not None
            and self._token_expires_at is not None
            and self._token_expires_at - _TOKEN_REFRESH_MARGIN > now
        )

    async def _request_token(self) -> dict[str, Any]:
        try:
            response = await self._client.post(
                f"{self._api_base_url}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
                timeout=self._timeout_seconds,
            )
        except httpx.RequestError as error:
            raise FeishuTransportError(
                "FEISHU_TOKEN_REQUEST_FAILED",
                response_payload={"exception": type(error).__name__},
                retryable=True,
            ) from error
        if response.status_code == 429:
            raise FeishuTransportError(
                "FEISHU_RATE_LIMITED",
                response_payload=_response_object_or_status(response),
                retryable=True,
            )
        if 400 <= response.status_code < 500:
            payload = _response_object_or_status(response)
            if _is_rate_limited_payload(payload):
                raise FeishuTransportError(
                    "FEISHU_RATE_LIMITED",
                    response_payload=payload,
                    retryable=True,
                )
            raise FeishuTransportError("FEISHU_AUTH_FAILED", response_payload=payload)
        payload = _response_object(
            response,
            invalid_response_retryable=response.status_code >= 500,
            invalid_response_outcome_unknown=False,
        )
        if _is_rate_limited_payload(payload):
            raise FeishuTransportError(
                "FEISHU_RATE_LIMITED",
                response_payload=payload,
                retryable=True,
            )
        if response.status_code >= 500:
            raise FeishuTransportError(
                "FEISHU_TOKEN_SERVER_ERROR",
                response_payload=payload,
                retryable=True,
            )
        if payload.get("code") != 0:
            raise FeishuTransportError("FEISHU_AUTH_FAILED", response_payload=payload)
        return payload

    async def _send_card_with_token(
        self,
        *,
        chat_id: str,
        content: str,
        request_uuid: str,
        token: str,
        allow_token_refresh: bool,
    ) -> FeishuSendResult:
        try:
            response = await self._client.post(
                f"{self._api_base_url}/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._timeout_seconds,
                json={
                    "receive_id": chat_id,
                    "msg_type": "interactive",
                    "content": content,
                    "uuid": request_uuid,
                },
            )
        except httpx.RequestError as error:
            raise FeishuTransportError(
                "FEISHU_SEND_OUTCOME_UNKNOWN",
                response_payload={"exception": type(error).__name__},
                outcome_unknown=True,
            ) from error

        if response.status_code == 401 and allow_token_refresh:
            self._tenant_access_token = None
            self._token_expires_at = None
            refreshed = await self._tenant_token()
            return await self._send_card_with_token(
                chat_id=chat_id,
                content=content,
                request_uuid=request_uuid,
                token=refreshed,
                allow_token_refresh=False,
            )
        if response.status_code in (401, 403):
            raise FeishuTransportError(
                "FEISHU_AUTH_FAILED",
                response_payload=_response_object_or_status(response),
            )
        if response.status_code == 429:
            raise FeishuTransportError(
                "FEISHU_RATE_LIMITED",
                response_payload=_response_object_or_status(response),
                retryable=True,
            )
        payload = _response_object(
            response,
            invalid_response_retryable=False,
            invalid_response_outcome_unknown=(response.is_success or response.status_code >= 500),
        )
        if _is_rate_limited_payload(payload):
            raise FeishuTransportError(
                "FEISHU_RATE_LIMITED",
                response_payload=payload,
                retryable=True,
            )
        if response.status_code >= 500:
            raise FeishuTransportError(
                "FEISHU_SEND_OUTCOME_UNKNOWN",
                response_payload=payload,
                outcome_unknown=True,
            )
        if response.status_code >= 400:
            raise FeishuTransportError(
                _classify_send_failure(payload, fallback="FEISHU_SEND_HTTP_ERROR"),
                response_payload=payload,
            )
        if payload.get("code") != 0:
            raise FeishuTransportError(
                _classify_send_failure(payload, fallback="FEISHU_SEND_REJECTED"),
                response_payload=payload,
            )

        data = payload.get("data")
        message_id = data.get("message_id") if isinstance(data, dict) else None
        if not isinstance(message_id, str) or not message_id:
            raise FeishuTransportError(
                "FEISHU_MESSAGE_ID_MISSING",
                response_payload=payload,
                outcome_unknown=True,
            )
        return FeishuSendResult(message_id=message_id, response_payload=payload)


class ReportDeliveryStore(Protocol):
    async def load_report(self, report_id: str) -> StoredDailyReport | None: ...

    async def reserve_delivery_attempt(self, attempt: DeliveryAttempt) -> bool: ...

    async def update_delivery_attempt(
        self,
        *,
        delivery_id: UUID,
        expected_attempt_no: int,
        status: Literal["failed", "retry_wait", "succeeded", "uncertain"],
        response_payload: dict[str, Any] | None,
        message_id: str | None = None,
        error_code: str | None = None,
    ) -> bool: ...

    async def retry_delivery_attempt(self, delivery_id: UUID) -> bool: ...

    async def authorize_manual_retry(
        self,
        delivery_id: UUID,
        *,
        expected_attempt_no: int,
        allow_uncertain: bool,
    ) -> bool: ...

    async def load_delivery_attempt(self, delivery_id: UUID) -> DeliveryAttempt | None: ...

    async def load_delivery_attempt_for_key(
        self,
        *,
        report_id: str,
        delivery_target: str,
        idempotency_key: str,
    ) -> DeliveryAttempt | None: ...


class PostgresReportDeliveryStore:
    """Commit every delivery state transition before returning to the network layer."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def load_report(self, report_id: str) -> StoredDailyReport | None:
        async with self._database.session() as session:
            return await ReportRepository(session).load_report(report_id)

    async def reserve_delivery_attempt(self, attempt: DeliveryAttempt) -> bool:
        async with UnitOfWork(self._database).transaction() as session:
            return await ReportRepository(session).reserve_delivery_attempt(attempt)

    async def update_delivery_attempt(
        self,
        *,
        delivery_id: UUID,
        expected_attempt_no: int,
        status: Literal["failed", "retry_wait", "succeeded", "uncertain"],
        response_payload: dict[str, Any] | None,
        message_id: str | None = None,
        error_code: str | None = None,
    ) -> bool:
        async with UnitOfWork(self._database).transaction() as session:
            return await ReportRepository(session).update_delivery_attempt(
                delivery_id=delivery_id,
                expected_attempt_no=expected_attempt_no,
                status=status,
                response_payload=response_payload,
                message_id=message_id,
                error_code=error_code,
            )

    async def retry_delivery_attempt(self, delivery_id: UUID) -> bool:
        async with UnitOfWork(self._database).transaction() as session:
            return await ReportRepository(session).retry_delivery_attempt(delivery_id)

    async def authorize_manual_retry(
        self,
        delivery_id: UUID,
        *,
        expected_attempt_no: int,
        allow_uncertain: bool,
    ) -> bool:
        async with UnitOfWork(self._database).transaction() as session:
            return await ReportRepository(session).authorize_manual_delivery_retry(
                delivery_id,
                expected_attempt_no=expected_attempt_no,
                allow_uncertain=allow_uncertain,
            )

    async def load_delivery_attempt(self, delivery_id: UUID) -> DeliveryAttempt | None:
        async with self._database.session() as session:
            return await ReportRepository(session).load_delivery_attempt(delivery_id)

    async def load_delivery_attempt_for_key(
        self,
        *,
        report_id: str,
        delivery_target: str,
        idempotency_key: str,
    ) -> DeliveryAttempt | None:
        async with self._database.session() as session:
            return await ReportRepository(session).load_delivery_attempt_for_key(
                report_id=report_id,
                delivery_target=delivery_target,
                idempotency_key=idempotency_key,
            )


@dataclass(frozen=True)
class ReportDeliveryResult:
    report_id: str
    report_version: str
    delivery_target: str
    idempotency_key: str
    card: dict[str, Any]
    dry_run: bool
    delivery_attempt: DeliveryAttempt | None

    @property
    def status(self) -> str:
        if self.dry_run:
            return "dry_run"
        return self.delivery_attempt.status if self.delivery_attempt else ""


class ReportDeliveryService:
    """Deliver one validated report to a Feishu chat with durable idempotency."""

    def __init__(
        self,
        transport: FeishuCardTransport,
        *,
        card_renderer: FeishuCardRenderer | None = None,
        max_attempts: int = 3,
        max_total_attempts: int = 10,
        retry_delay_seconds: float = 0.25,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("Feishu delivery max_attempts must be positive")
        if retry_delay_seconds < 0:
            raise ValueError("Feishu delivery retry delay must not be negative")
        if max_total_attempts < max_attempts:
            raise ValueError("Feishu delivery max_total_attempts must cover automatic attempts")
        self._transport = transport
        self._card_renderer = card_renderer or FeishuCardRenderer()
        self._max_attempts = max_attempts
        self._max_total_attempts = max_total_attempts
        self._retry_delay_seconds = retry_delay_seconds
        self._sleep = sleep

    async def deliver(
        self,
        store: ReportDeliveryStore,
        *,
        report_id: str,
        chat_id: str,
        dry_run: bool = False,
    ) -> ReportDeliveryResult:
        report = await store.load_report(report_id)
        if report is None:
            raise ReportDeliveryError(f"report does not exist: {report_id}")
        if report.lifecycle_status != "validated" or report.publication_decision != "published":
            raise ReportDeliveryError("only validated published reports may be delivered")
        if not chat_id.strip():
            raise ReportDeliveryError("Feishu chat ID is required")

        card = self._card_renderer.render(report)
        delivery_target = f"feishu:{chat_id}"
        idempotency_key = _idempotency_key(report, delivery_target)
        request_uuid = _feishu_request_uuid(idempotency_key)
        if dry_run:
            return ReportDeliveryResult(
                report_id=report.report_id,
                report_version=report.report_version,
                delivery_target=delivery_target,
                idempotency_key=idempotency_key,
                card=card,
                dry_run=True,
                delivery_attempt=None,
            )

        initial_attempt = DeliveryAttempt(
            delivery_id=uuid4(),
            report_id=report.report_id,
            report_version=report.report_version,
            delivery_target=delivery_target,
            idempotency_key=idempotency_key,
            request_payload=_request_audit_payload(
                report,
                delivery_target,
                card,
                request_uuid=request_uuid,
            ),
        )
        inserted = await store.reserve_delivery_attempt(initial_attempt)
        if inserted:
            attempt = initial_attempt
        else:
            existing = await store.load_delivery_attempt_for_key(
                report_id=report.report_id,
                delivery_target=delivery_target,
                idempotency_key=idempotency_key,
            )
            if existing is None:
                raise ReportDeliveryError("delivery idempotency conflict has no stored attempt")
            if existing.status != "retry_wait" or existing.attempt_no >= self._max_attempts:
                return _result(report, delivery_target, idempotency_key, card, existing)
            resumed_attempt = await self._resume_retry(store, existing)
            if resumed_attempt is None:
                current = await store.load_delivery_attempt(existing.delivery_id)
                if current is None:
                    raise ReportDeliveryError("resumed delivery attempt no longer exists")
                return _result(report, delivery_target, idempotency_key, card, current)
            attempt = resumed_attempt

        return await self._send(
            store,
            report=report,
            chat_id=chat_id,
            card=card,
            delivery_target=delivery_target,
            idempotency_key=idempotency_key,
            request_uuid=request_uuid,
            attempt=attempt,
            max_attempt_no=self._max_attempts,
        )

    async def retry(
        self,
        store: ReportDeliveryStore,
        *,
        report_id: str,
        chat_id: str,
        confirmed_not_delivered: bool,
    ) -> ReportDeliveryResult:
        report = await store.load_report(report_id)
        if report is None:
            raise ManualDeliveryRetryError(
                "REPORT_NOT_FOUND", f"report does not exist: {report_id}"
            )
        if report.lifecycle_status != "validated" or report.publication_decision != "published":
            raise ManualDeliveryRetryError(
                "REPORT_NOT_PUBLISHABLE",
                "only validated published reports may be retried",
            )
        if not chat_id.strip():
            raise ManualDeliveryRetryError(
                "DELIVERY_TARGET_NOT_CONFIGURED",
                "Feishu chat ID is required",
            )
        card = self._card_renderer.render(report)
        delivery_target = f"feishu:{chat_id}"
        idempotency_key = _idempotency_key(report, delivery_target)
        request_uuid = _feishu_request_uuid(idempotency_key)
        existing = await store.load_delivery_attempt_for_key(
            report_id=report.report_id,
            delivery_target=delivery_target,
            idempotency_key=idempotency_key,
        )
        if existing is None:
            raise ManualDeliveryRetryError(
                "DELIVERY_ATTEMPT_NOT_FOUND",
                "the report has no delivery attempt to retry",
            )
        if existing.status == "succeeded":
            return _result(report, delivery_target, idempotency_key, card, existing)
        if existing.status == "pending":
            raise ManualDeliveryRetryError(
                "DELIVERY_ATTEMPT_IN_PROGRESS",
                "the existing delivery attempt is still pending",
            )
        if existing.status == "uncertain" and not confirmed_not_delivered:
            raise ManualDeliveryRetryError(
                "DELIVERY_ABSENCE_CONFIRMATION_REQUIRED",
                "confirm that the report is absent from the daily chat before retrying",
            )
        if existing.attempt_no >= self._max_total_attempts:
            raise ManualDeliveryRetryError(
                "DELIVERY_RETRY_LIMIT_EXHAUSTED",
                "the total delivery retry limit is exhausted",
            )
        if existing.status not in {"failed", "retry_wait", "uncertain"}:
            raise ManualDeliveryRetryError(
                "DELIVERY_ATTEMPT_NOT_RETRYABLE",
                f"delivery status cannot be retried: {existing.status}",
            )
        authorized = await store.authorize_manual_retry(
            existing.delivery_id,
            expected_attempt_no=existing.attempt_no,
            allow_uncertain=confirmed_not_delivered,
        )
        if not authorized:
            raise ManualDeliveryRetryError(
                "DELIVERY_RETRY_CONFLICT",
                "the delivery attempt changed while retry was being authorized",
            )
        attempt = existing.model_copy(
            update={
                "attempt_no": existing.attempt_no + 1,
                "status": "pending",
                "response_payload": None,
                "message_id": None,
                "error_code": None,
            }
        )
        return await self._send(
            store,
            report=report,
            chat_id=chat_id,
            card=card,
            delivery_target=delivery_target,
            idempotency_key=idempotency_key,
            request_uuid=request_uuid,
            attempt=attempt,
            max_attempt_no=attempt.attempt_no,
        )

    async def inspect(
        self,
        store: ReportDeliveryStore,
        *,
        report_id: str,
        chat_id: str,
    ) -> ReportDeliveryResult:
        report = await store.load_report(report_id)
        if report is None:
            raise ManualDeliveryRetryError(
                "REPORT_NOT_FOUND", f"report does not exist: {report_id}"
            )
        if report.lifecycle_status != "validated" or report.publication_decision != "published":
            raise ManualDeliveryRetryError(
                "REPORT_NOT_PUBLISHABLE",
                "only validated published reports may be inspected for delivery recovery",
            )
        if not chat_id.strip():
            raise ManualDeliveryRetryError(
                "DELIVERY_TARGET_NOT_CONFIGURED",
                "Feishu chat ID is required",
            )
        card = self._card_renderer.render(report)
        delivery_target = f"feishu:{chat_id}"
        idempotency_key = _idempotency_key(report, delivery_target)
        attempt = await store.load_delivery_attempt_for_key(
            report_id=report.report_id,
            delivery_target=delivery_target,
            idempotency_key=idempotency_key,
        )
        return _result(report, delivery_target, idempotency_key, card, attempt)

    async def _send(
        self,
        store: ReportDeliveryStore,
        *,
        report: StoredDailyReport,
        chat_id: str,
        card: dict[str, Any],
        delivery_target: str,
        idempotency_key: str,
        request_uuid: str,
        attempt: DeliveryAttempt,
        max_attempt_no: int,
    ) -> ReportDeliveryResult:
        while True:
            try:
                sent = await self._transport.send_card(
                    chat_id=chat_id,
                    card=card,
                    request_uuid=request_uuid,
                )
            except FeishuTransportError as error:
                next_status: Literal["failed", "retry_wait", "uncertain"]
                if error.outcome_unknown:
                    next_status = "uncertain"
                elif error.retryable and attempt.attempt_no < max_attempt_no:
                    next_status = "retry_wait"
                else:
                    next_status = "failed"
                updated = await store.update_delivery_attempt(
                    delivery_id=attempt.delivery_id,
                    expected_attempt_no=attempt.attempt_no,
                    status=next_status,
                    response_payload=_failure_audit_payload(error),
                    error_code=error.error_code,
                )
                if not updated:
                    current = await store.load_delivery_attempt(attempt.delivery_id)
                    if current is None:
                        raise ReportDeliveryError(
                            "delivery attempt disappeared during update"
                        ) from error
                    return _result(report, delivery_target, idempotency_key, card, current)
                attempt = attempt.model_copy(
                    update={
                        "status": next_status,
                        "response_payload": _failure_audit_payload(error),
                        "message_id": None,
                        "error_code": error.error_code,
                    }
                )
                if next_status != "retry_wait":
                    return _result(report, delivery_target, idempotency_key, card, attempt)
                await self._sleep(self._retry_delay_seconds * (2 ** (attempt.attempt_no - 1)))
                resumed = await self._resume_retry(store, attempt)
                if resumed is None:
                    current = await store.load_delivery_attempt(attempt.delivery_id)
                    if current is None:
                        raise ReportDeliveryError(
                            "delivery attempt disappeared during retry"
                        ) from error
                    return _result(report, delivery_target, idempotency_key, card, current)
                attempt = resumed
            else:
                response_payload = {
                    "provider": "feishu",
                    "result": "succeeded",
                    "response": _redact(sent.response_payload),
                }
                updated = await store.update_delivery_attempt(
                    delivery_id=attempt.delivery_id,
                    expected_attempt_no=attempt.attempt_no,
                    status="succeeded",
                    response_payload=response_payload,
                    message_id=sent.message_id,
                )
                if not updated:
                    current = await store.load_delivery_attempt(attempt.delivery_id)
                    if current is None:
                        raise ReportDeliveryError(
                            "delivery attempt disappeared during success update"
                        )
                    return _result(report, delivery_target, idempotency_key, card, current)
                succeeded = attempt.model_copy(
                    update={
                        "status": "succeeded",
                        "response_payload": response_payload,
                        "message_id": sent.message_id,
                        "error_code": None,
                    }
                )
                return _result(report, delivery_target, idempotency_key, card, succeeded)

    async def _resume_retry(
        self,
        store: ReportDeliveryStore,
        attempt: DeliveryAttempt,
    ) -> DeliveryAttempt | None:
        resumed = await store.retry_delivery_attempt(attempt.delivery_id)
        if not resumed:
            return None
        return attempt.model_copy(
            update={
                "attempt_no": attempt.attempt_no + 1,
                "status": "pending",
                "response_payload": None,
                "message_id": None,
                "error_code": None,
            }
        )


class ConfiguredFeishuDelivery:
    """Binds enabled Feishu runtime configuration to the delivery service.

    The scheduler owns the HTTP-client lifecycle and invokes this thin adapter
    after a report has passed validation.  Keeping that invocation outside of
    this module avoids creating a second scheduler while still ensuring that
    application credentials, target chat, timeout, and retry policy come only
    from runtime configuration.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        client: httpx.AsyncClient,
        store: ReportDeliveryStore,
    ) -> None:
        if not settings.feishu_delivery_enabled:
            raise ValueError("Feishu delivery is disabled")
        if (
            settings.feishu_app_id is None
            or settings.feishu_app_secret is None
            or settings.feishu_chat_id is None
        ):
            raise ValueError("enabled Feishu delivery requires complete configuration")
        self._store = store
        self._chat_id = settings.feishu_chat_id
        self._service = ReportDeliveryService(
            FeishuTransport(
                app_id=settings.feishu_app_id,
                app_secret=settings.feishu_app_secret,
                client=client,
                api_base_url=settings.feishu_api_base_url,
                timeout_seconds=settings.feishu_timeout_seconds,
            ),
            max_attempts=settings.feishu_delivery_max_attempts,
            max_total_attempts=settings.feishu_delivery_max_total_attempts,
        )

    async def deliver(
        self,
        *,
        report_id: str,
        dry_run: bool = False,
    ) -> ReportDeliveryResult:
        return await self._service.deliver(
            self._store,
            report_id=report_id,
            chat_id=self._chat_id,
            dry_run=dry_run,
        )

    async def retry(
        self,
        *,
        report_id: str,
        confirmed_not_delivered: bool,
    ) -> ReportDeliveryResult:
        return await self._service.retry(
            self._store,
            report_id=report_id,
            chat_id=self._chat_id,
            confirmed_not_delivered=confirmed_not_delivered,
        )

    async def inspect(self, *, report_id: str) -> ReportDeliveryResult:
        return await self._service.inspect(
            self._store,
            report_id=report_id,
            chat_id=self._chat_id,
        )


def _section_display_text(report: DailyReport, section_id: str) -> str:
    section = report.sections[section_id]
    if section.text:
        return section.text
    item_lines = _section_item_lines(report, section_id)
    if item_lines:
        return "\n".join(f"• {line}" for line in item_lines)
    if section.reason_code:
        return f"暂无可用数据（{section.reason_code}）"
    return "暂无可用数据"


def _summary_display_text(report: DailyReport) -> str:
    section = report.sections["executive_summary"]
    if section.text and section.reason_code != "NO_VERIFIED_FACTS":
        return section.text
    if lines := _section_item_lines(report, "executive_summary"):
        return "\n".join(f"• {line}" for line in lines)

    summary_lines: list[str] = []
    for label, section_id in (
        ("中国内地", "cn_highlights"),
        ("香港", "hk_highlights"),
        ("美国", "us_highlights"),
    ):
        lines = _section_item_lines(report, section_id)
        if lines:
            summary_lines.append(f"• {label}：{lines[0]}")
    calendar_lines = _calendar_item_lines(report)
    if calendar_lines:
        summary_lines.append(f"• 未来日程：{calendar_lines[0].removeprefix('• ')}")
    return (
        "\n".join(summary_lines)
        if summary_lines
        else _section_display_text(report, "executive_summary")
    )


def _section_item_lines(report: DailyReport, section_id: str) -> list[str]:
    lines: list[str] = []
    for item in report.sections[section_id].items:
        visible = item.get("text") or item.get("label") or item.get("name")
        if isinstance(visible, str) and visible.strip():
            lines.append(visible.strip())
    return lines


def _calendar_display_text(report: DailyReport) -> str:
    lines = _calendar_item_lines(report)
    if lines:
        return "\n".join(lines)
    return _section_display_text(report, "upcoming_calendar")


def _calendar_item_lines(report: DailyReport) -> list[str]:
    section = report.sections["upcoming_calendar"]
    lines: list[str] = []
    for item in section.items:
        region = item.get("region")
        name = item.get("name")
        scheduled_at = item.get("scheduled_at") or item.get("scheduled_date")
        if (
            not isinstance(region, str)
            or not region
            or not isinstance(name, str)
            or not name
            or not isinstance(scheduled_at, str)
            or not scheduled_at
        ):
            continue
        lines.append(f"• {_format_scheduled_at(scheduled_at)}（{region}）{name}")
    if not lines:
        lines = [f"• {line}" for line in _section_item_lines(report, "upcoming_calendar")]
    return lines


def _source_display_text(report: DailyReport) -> str:
    source_items = report.sections["source_references"].items
    sources = [ReportSourceReference.model_validate(item) for item in source_items]
    if not sources:
        return "报告未提供来源链接"
    unique_sources: list[ReportSourceReference] = []
    seen_names: set[str] = set()
    for source in sources:
        normalized_name = source.source_name.strip().casefold()
        if normalized_name in seen_names:
            continue
        seen_names.add(normalized_name)
        unique_sources.append(source)
    return "\n".join(
        f"• [{source.source_name}]({source.source_url})"
        if source.source_url is not None
        else f"• {source.source_name}"
        for source in unique_sources
    )


def _format_scheduled_at(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        return value
    return parsed.astimezone(_REPORT_TIMEZONE).strftime("%Y-%m-%d %H:%M")


def _idempotency_key(report: StoredDailyReport, delivery_target: str) -> str:
    scope = "\x00".join((report.report_date.isoformat(), delivery_target, report.report_version))
    return f"feishu:{hashlib.sha256(scope.encode()).hexdigest()}"


def _feishu_request_uuid(idempotency_key: str) -> str:
    return hashlib.sha256(idempotency_key.encode()).hexdigest()[:50]


def _request_audit_payload(
    report: StoredDailyReport,
    delivery_target: str,
    card: Mapping[str, Any],
    *,
    request_uuid: str,
) -> dict[str, Any]:
    serialized_card = json.dumps(card, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "provider": "feishu",
        "delivery_target": delivery_target,
        "report_date": report.report_date.isoformat(),
        "report_version": report.report_version,
        "message_type": "interactive",
        "request_uuid": request_uuid,
        "card_sha256": hashlib.sha256(serialized_card.encode()).hexdigest(),
    }


def _failure_audit_payload(error: FeishuTransportError) -> dict[str, Any]:
    return {
        "provider": "feishu",
        "result": "uncertain" if error.outcome_unknown else "failed",
        "error_code": error.error_code,
        "response": _redact(error.response_payload) if error.response_payload is not None else None,
    }


def _response_object(
    response: httpx.Response,
    *,
    invalid_response_retryable: bool,
    invalid_response_outcome_unknown: bool,
) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise FeishuTransportError(
            "FEISHU_RESPONSE_INVALID",
            response_payload={"http_status": response.status_code},
            retryable=invalid_response_retryable,
            outcome_unknown=invalid_response_outcome_unknown,
        ) from error
    if not isinstance(payload, dict):
        raise FeishuTransportError(
            "FEISHU_RESPONSE_INVALID",
            response_payload={"http_status": response.status_code},
            retryable=invalid_response_retryable,
            outcome_unknown=invalid_response_outcome_unknown,
        )
    return cast(dict[str, Any], payload)


def _response_object_or_status(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"http_status": response.status_code}
    if not isinstance(payload, dict):
        return {"http_status": response.status_code}
    return cast(dict[str, Any], payload)


def _is_rate_limited_payload(payload: Mapping[str, Any]) -> bool:
    return payload.get("code") in _FEISHU_RATE_LIMIT_CODES


def _classify_send_failure(payload: Mapping[str, Any], *, fallback: str) -> str:
    """Map stable Feishu message failures to audit-friendly error codes.

    Feishu uses error code 230001 for several invalid-parameter cases, so the
    documented diagnostic text is used only to split card-content and target
    chat errors; everything else remains a generic invalid request rather than
    risking an unsafe automatic retry.
    """

    code = payload.get("code")
    if code in _FEISHU_AUTH_CODES:
        return "FEISHU_AUTH_FAILED"
    if code in _FEISHU_CARD_INVALID_CODES:
        return "FEISHU_CARD_INVALID"
    if code in _FEISHU_CHAT_UNAVAILABLE_CODES:
        return "FEISHU_CHAT_UNAVAILABLE"
    if code in _FEISHU_RATE_LIMIT_CODES:
        return "FEISHU_RATE_LIMITED"
    if code == 230001:
        message = " ".join(
            value
            for value in (payload.get("msg"), payload.get("message"))
            if isinstance(value, str)
        ).lower()
        if any(token in message for token in ("card", "content", "interactive", "schema")):
            return "FEISHU_CARD_INVALID"
        if any(token in message for token in ("receive_id", "chat_id", "group", "群")):
            return "FEISHU_CHAT_UNAVAILABLE"
        return "FEISHU_REQUEST_INVALID"
    return fallback


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_sensitive_key(key) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    normalized = "".join(character for character in key.lower() if character.isalnum())
    return normalized in _REDACTED_KEY_NAMES


def _result(
    report: StoredDailyReport,
    delivery_target: str,
    idempotency_key: str,
    card: dict[str, Any],
    attempt: DeliveryAttempt | None,
) -> ReportDeliveryResult:
    return ReportDeliveryResult(
        report_id=report.report_id,
        report_version=report.report_version,
        delivery_target=delivery_target,
        idempotency_key=idempotency_key,
        card=card,
        dry_run=False,
        delivery_attempt=attempt,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)
