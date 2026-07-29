from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from macro_platform.config import Settings
from macro_platform.services.report_delivery import (
    ConfiguredFeishuDelivery,
    FeishuCardRenderer,
    FeishuSendResult,
    FeishuTransport,
    FeishuTransportError,
    ManualDeliveryRetryError,
    ReportDeliveryError,
    ReportDeliveryService,
)
from macro_platform.storage.reporting import DeliveryAttempt, StoredDailyReport

ROOT = Path(__file__).parents[2]


def _report(*, publication_decision: str = "published") -> StoredDailyReport:
    payload = json.loads((ROOT / "tests/golden/daily_report_v1_success.json").read_text())
    payload["publication"]["decision"] = publication_decision
    if publication_decision != "published":
        payload["publication"]["published_at"] = None
    return StoredDailyReport(
        report_id=payload["report_id"],
        report_date=payload["report_date"],
        report_version="v1",
        contract_version=payload["contract_version"],
        input_snapshot_id=payload["input_snapshot"]["snapshot_id"],
        status=payload["status"],
        publication_decision=publication_decision,  # type: ignore[arg-type]
        generated_at=payload["generated_at"],
        payload=payload,
        lifecycle_status="validated",
    )


class _Store:
    def __init__(self, report: StoredDailyReport) -> None:
        self.report = report
        self.attempts: dict[UUID, DeliveryAttempt] = {}
        self.keys: dict[tuple[str, str, str], UUID] = {}
        self.reserve_calls = 0

    async def load_report(self, report_id: str) -> StoredDailyReport | None:
        return self.report if report_id == self.report.report_id else None

    async def reserve_delivery_attempt(self, attempt: DeliveryAttempt) -> bool:
        self.reserve_calls += 1
        key = (attempt.report_id, attempt.delivery_target, attempt.idempotency_key)
        if key in self.keys:
            existing = self.attempts[self.keys[key]]
            if existing.request_payload != attempt.request_payload:
                raise ValueError("delivery key reused with a different request")
            return False
        self.attempts[attempt.delivery_id] = attempt
        self.keys[key] = attempt.delivery_id
        return True

    async def update_delivery_attempt(
        self,
        *,
        delivery_id: UUID,
        expected_attempt_no: int,
        status: str,
        response_payload: dict[str, Any] | None,
        message_id: str | None = None,
        error_code: str | None = None,
    ) -> bool:
        current = self.attempts.get(delivery_id)
        if (
            current is None
            or current.attempt_no != expected_attempt_no
            or current.status != "pending"
        ):
            return False
        self.attempts[delivery_id] = current.model_copy(
            update={
                "status": status,
                "response_payload": response_payload,
                "message_id": message_id,
                "error_code": error_code,
            }
        )
        return True

    async def retry_delivery_attempt(self, delivery_id: UUID) -> bool:
        current = self.attempts.get(delivery_id)
        if current is None or current.status != "retry_wait":
            return False
        self.attempts[delivery_id] = current.model_copy(
            update={
                "attempt_no": current.attempt_no + 1,
                "status": "pending",
                "response_payload": None,
                "message_id": None,
                "error_code": None,
            }
        )
        return True

    async def authorize_manual_retry(
        self,
        delivery_id: UUID,
        *,
        expected_attempt_no: int,
        allow_uncertain: bool,
    ) -> bool:
        current = self.attempts.get(delivery_id)
        allowed = {"failed", "retry_wait"}
        if allow_uncertain:
            allowed.add("uncertain")
        if (
            current is None
            or current.attempt_no != expected_attempt_no
            or current.status not in allowed
        ):
            return False
        self.attempts[delivery_id] = current.model_copy(
            update={
                "attempt_no": current.attempt_no + 1,
                "status": "pending",
                "response_payload": None,
                "message_id": None,
                "error_code": None,
            }
        )
        return True

    async def load_delivery_attempt(self, delivery_id: UUID) -> DeliveryAttempt | None:
        return self.attempts.get(delivery_id)

    async def load_delivery_attempt_for_key(
        self,
        *,
        report_id: str,
        delivery_target: str,
        idempotency_key: str,
    ) -> DeliveryAttempt | None:
        delivery_id = self.keys.get((report_id, delivery_target, idempotency_key))
        return self.attempts.get(delivery_id) if delivery_id is not None else None


class _Transport:
    def __init__(self, responses: list[FeishuSendResult | FeishuTransportError]) -> None:
        self._responses = responses
        self.requests: list[tuple[str, Mapping[str, Any], str]] = []

    async def send_card(
        self,
        *,
        chat_id: str,
        card: Mapping[str, Any],
        request_uuid: str,
    ) -> FeishuSendResult:
        self.requests.append((chat_id, card, request_uuid))
        result = self._responses.pop(0)
        if isinstance(result, FeishuTransportError):
            raise result
        return result


def test_report_delivery_card_matches_frozen_golden_fixture() -> None:
    card = FeishuCardRenderer().render(_report())
    expected = json.loads((ROOT / "tests/golden/daily_report_v1_feishu_card.json").read_text())

    assert card == expected


def test_report_delivery_renders_fallback_items_summary_and_unique_sources() -> None:
    report = _report()
    payload = json.loads(json.dumps(report.payload))
    payload["sections"]["executive_summary"] = {
        "section_id": "executive_summary",
        "status": "degraded",
        "character_count": len("暂无已验证事实。"),
        "max_characters": 800,
        "text": "暂无已验证事实。",
        "reason_code": "NO_VERIFIED_FACTS",
    }
    region_facts = (
        (
            "cn_highlights",
            "沪深300 收盘 4525.16 CNY。",
            "fact.market.cn.core_indices.previous_close",
            "src-cn-market-1",
        ),
        (
            "hk_highlights",
            "HSI 收盘 25602.76 HKD。",
            "fact.market.hk.core_indices.previous_close",
            "src-hk-market-1",
        ),
        (
            "us_highlights",
            "SPX 收盘 6389.77 USD。",
            "fact.market.us.core_indices.previous_close",
            "src-us-market-1",
        ),
    )
    for section_id, text, fact_id, source_ref_id in region_facts:
        label = text.split(" 收盘", maxsplit=1)[0]
        payload["sections"][section_id] = {
            "section_id": section_id,
            "status": "complete",
            "character_count": len(label) + len(text),
            "max_characters": 1000,
            "items": [
                {
                    "label": label,
                    "text": text,
                    "fact_ids": [fact_id],
                    "source_ref_ids": [source_ref_id],
                }
            ],
        }
    calendar_label = "美国 PCE"
    calendar_text = "美国 PCE（US）：计划于 2026-07-30 发布。"
    payload["sections"]["upcoming_calendar"] = {
        "section_id": "upcoming_calendar",
        "status": "complete",
        "character_count": len(calendar_label) + len(calendar_text),
        "max_characters": 1600,
        "lookahead_days": 7,
        "items": [
            {
                "label": calendar_label,
                "text": calendar_text,
                "fact_ids": ["fact.calendar.us.pce_20260729"],
                "source_ref_ids": ["src-calendar-1"],
            }
        ],
    }
    duplicate_source = {
        **payload["sections"]["source_references"]["items"][0],
        "source_ref_id": "src-cn-market-duplicate",
        "provider_record_id": "cn-market:duplicate",
        "checksum_sha256": "a" * 64,
    }
    payload["sections"]["source_references"]["items"].append(duplicate_source)
    fallback_report = report.model_copy(update={"payload": payload})

    card = FeishuCardRenderer().render(fallback_report)
    contents = [
        element["content"] for element in card["body"]["elements"] if element["tag"] == "markdown"
    ]

    assert contents[0] == (
        "**摘要**\n"
        "• 中国内地：沪深300 收盘 4525.16 CNY。\n"
        "• 香港：HSI 收盘 25602.76 HKD。\n"
        "• 美国：SPX 收盘 6389.77 USD。\n"
        "• 未来日程：美国 PCE（US）：计划于 2026-07-30 发布。"
    )
    assert contents[2] == "**香港**\n• HSI 收盘 25602.76 HKD。"
    assert contents[4] == "**未来日程**\n• 美国 PCE（US）：计划于 2026-07-30 发布。"
    source_content = contents[6]
    assert source_content.count("CN market daily fixture") == 1


async def test_report_delivery_dry_run_has_no_side_effect() -> None:
    report = _report()
    store = _Store(report)
    transport = _Transport([])

    result = await ReportDeliveryService(transport).deliver(
        store,
        report_id=report.report_id,
        chat_id="oc_test",
        dry_run=True,
    )

    assert result.status == "dry_run"
    assert result.delivery_attempt is None
    assert store.reserve_calls == 0
    assert transport.requests == []


async def test_report_delivery_requires_validated_published_report() -> None:
    report = _report(publication_decision="not_published")
    store = _Store(report)

    with pytest.raises(ReportDeliveryError, match="only validated published"):
        await ReportDeliveryService(_Transport([])).deliver(
            store,
            report_id=report.report_id,
            chat_id="oc_test",
        )


async def test_report_delivery_stores_message_id_and_reuses_successful_attempt() -> None:
    report = _report()
    store = _Store(report)
    transport = _Transport(
        [FeishuSendResult(message_id="om_message_1", response_payload={"code": 0, "data": {}})]
    )
    service = ReportDeliveryService(transport)

    first = await service.deliver(store, report_id=report.report_id, chat_id="oc_test")
    second = await service.deliver(store, report_id=report.report_id, chat_id="oc_test")

    assert first.status == "succeeded"
    assert first.delivery_attempt is not None
    assert first.delivery_attempt.report_version == "v1"
    assert first.delivery_attempt.message_id == "om_message_1"
    assert first.delivery_attempt.request_payload["delivery_target"] == "feishu:oc_test"
    assert second.delivery_attempt == first.delivery_attempt
    assert len(transport.requests) == 1
    assert len(store.attempts) == 1


async def test_report_delivery_retries_only_known_safe_failure() -> None:
    report = _report()
    store = _Store(report)
    transport = _Transport(
        [
            FeishuTransportError("FEISHU_RATE_LIMITED", retryable=True),
            FeishuSendResult(message_id="om_message_2", response_payload={"code": 0}),
        ]
    )
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    result = await ReportDeliveryService(
        transport,
        max_attempts=2,
        retry_delay_seconds=0.1,
        sleep=record_sleep,
    ).deliver(store, report_id=report.report_id, chat_id="oc_test")

    assert result.status == "succeeded"
    assert result.delivery_attempt is not None
    assert result.delivery_attempt.attempt_no == 2
    assert delays == [0.1]
    assert len(transport.requests) == 2
    assert len({request_uuid for _, _, request_uuid in transport.requests}) == 1
    assert len(transport.requests[0][2]) == 50
    assert result.delivery_attempt.request_payload["request_uuid"] == transport.requests[0][2]


async def test_report_delivery_keeps_ambiguous_send_outcome_out_of_auto_retry() -> None:
    report = _report()
    store = _Store(report)
    transport = _Transport(
        [FeishuTransportError("FEISHU_SEND_OUTCOME_UNKNOWN", outcome_unknown=True)]
    )

    service = ReportDeliveryService(transport)
    result = await service.deliver(
        store,
        report_id=report.report_id,
        chat_id="oc_test",
    )
    replay = await service.deliver(store, report_id=report.report_id, chat_id="oc_test")

    assert result.status == "uncertain"
    assert result.delivery_attempt is not None
    assert result.delivery_attempt.error_code == "FEISHU_SEND_OUTCOME_UNKNOWN"
    assert replay.delivery_attempt == result.delivery_attempt
    assert len(transport.requests) == 1


async def test_report_delivery_persists_classified_failure_with_redacted_response() -> None:
    report = _report()
    store = _Store(report)
    transport = _Transport(
        [
            FeishuTransportError(
                "FEISHU_CARD_INVALID",
                response_payload={"code": 230001, "tenantAccessToken": "must-not-persist"},
            )
        ]
    )

    result = await ReportDeliveryService(transport).deliver(
        store,
        report_id=report.report_id,
        chat_id="oc_test",
    )

    assert result.status == "failed"
    assert result.delivery_attempt is not None
    assert result.delivery_attempt.error_code == "FEISHU_CARD_INVALID"
    assert result.delivery_attempt.response_payload == {
        "provider": "feishu",
        "result": "failed",
        "error_code": "FEISHU_CARD_INVALID",
        "response": {"code": 230001, "tenantAccessToken": "[REDACTED]"},
    }


async def test_manual_delivery_retry_sends_a_known_failed_attempt_once() -> None:
    report = _report()
    store = _Store(report)
    transport = _Transport(
        [
            FeishuTransportError("FEISHU_CARD_INVALID"),
            FeishuSendResult(message_id="om_manual_retry", response_payload={"code": 0}),
        ]
    )
    service = ReportDeliveryService(transport)

    failed = await service.deliver(store, report_id=report.report_id, chat_id="oc_test")
    recovered = await service.retry(
        store,
        report_id=report.report_id,
        chat_id="oc_test",
        confirmed_not_delivered=False,
    )

    assert failed.status == "failed"
    assert recovered.status == "succeeded"
    assert recovered.delivery_attempt is not None
    assert recovered.delivery_attempt.attempt_no == 2
    assert len(transport.requests) == 2
    assert transport.requests[0][2] == transport.requests[1][2]


async def test_manual_delivery_retry_requires_confirmation_for_an_uncertain_attempt() -> None:
    report = _report()
    store = _Store(report)
    transport = _Transport(
        [
            FeishuTransportError("FEISHU_SEND_OUTCOME_UNKNOWN", outcome_unknown=True),
            FeishuSendResult(message_id="om_confirmed_retry", response_payload={"code": 0}),
        ]
    )
    service = ReportDeliveryService(transport)
    uncertain = await service.deliver(
        store,
        report_id=report.report_id,
        chat_id="oc_test",
    )

    with pytest.raises(ManualDeliveryRetryError) as caught:
        await service.retry(
            store,
            report_id=report.report_id,
            chat_id="oc_test",
            confirmed_not_delivered=False,
        )
    recovered = await service.retry(
        store,
        report_id=report.report_id,
        chat_id="oc_test",
        confirmed_not_delivered=True,
    )

    assert uncertain.status == "uncertain"
    assert caught.value.code == "DELIVERY_ABSENCE_CONFIRMATION_REQUIRED"
    assert len(transport.requests) == 2
    assert recovered.status == "succeeded"


async def test_manual_delivery_retry_stops_at_the_total_attempt_limit() -> None:
    report = _report()
    store = _Store(report)
    transport = _Transport([FeishuTransportError("FEISHU_CHAT_UNAVAILABLE")])
    service = ReportDeliveryService(
        transport,
        max_attempts=1,
        max_total_attempts=1,
    )
    failed = await service.deliver(
        store,
        report_id=report.report_id,
        chat_id="oc_test",
    )

    with pytest.raises(ManualDeliveryRetryError) as caught:
        await service.retry(
            store,
            report_id=report.report_id,
            chat_id="oc_test",
            confirmed_not_delivered=False,
        )

    assert failed.status == "failed"
    assert caught.value.code == "DELIVERY_RETRY_LIMIT_EXHAUSTED"
    assert len(transport.requests) == 1


async def test_feishu_transport_caches_token_and_sends_interactive_card() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_message"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = FeishuTransport(
            app_id="cli_test",
            app_secret=SecretStr("secret-test"),
            client=client,
        )
        card = FeishuCardRenderer().render(_report())
        first = await transport.send_card(chat_id="oc_test", card=card, request_uuid="a" * 50)
        second = await transport.send_card(chat_id="oc_test", card=card, request_uuid="a" * 50)

    assert first.message_id == "om_message"
    assert second.message_id == "om_message"
    assert len(requests) == 3
    assert requests[1].headers["Authorization"] == "Bearer tenant-token"
    body = json.loads(requests[1].content)
    assert body["receive_id"] == "oc_test"
    assert body["msg_type"] == "interactive"
    assert body["uuid"] == "a" * 50
    assert json.loads(body["content"]) == card


async def test_feishu_transport_refreshes_token_once_after_unauthorized_response() -> None:
    requests: list[httpx.Request] = []
    token_count = 0
    message_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_count, message_count
        requests.append(request)
        if request.url.path.endswith("tenant_access_token/internal"):
            token_count += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": f"tenant-token-{token_count}",
                    "expire": 7200,
                },
            )
        message_count += 1
        if message_count == 1:
            return httpx.Response(401, json={"code": 99991663, "msg": "token expired"})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_refreshed"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = FeishuTransport(
            app_id="cli_test",
            app_secret="secret-test",
            client=client,
        )
        result = await transport.send_card(
            chat_id="oc_test",
            card=FeishuCardRenderer().render(_report()),
            request_uuid="b" * 50,
        )

    assert result.message_id == "om_refreshed"
    assert token_count == 2
    assert message_count == 2
    assert requests[-1].headers["Authorization"] == "Bearer tenant-token-2"


async def test_feishu_transport_marks_non_json_server_response_as_ambiguous() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        return httpx.Response(500, text="gateway failure")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = FeishuTransport(
            app_id="cli_test",
            app_secret="secret-test",
            client=client,
        )
        with pytest.raises(FeishuTransportError) as caught:
            await transport.send_card(
                chat_id="oc_test",
                card=FeishuCardRenderer().render(_report()),
                request_uuid="c" * 50,
            )

    assert caught.value.error_code == "FEISHU_RESPONSE_INVALID"
    assert caught.value.outcome_unknown is True
    assert caught.value.retryable is False


async def test_feishu_transport_classifies_non_json_http_rate_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        return httpx.Response(429, text="rate limited")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = FeishuTransport(
            app_id="cli_test",
            app_secret="secret-test",
            client=client,
        )
        with pytest.raises(FeishuTransportError) as caught:
            await transport.send_card(
                chat_id="oc_test",
                card=FeishuCardRenderer().render(_report()),
                request_uuid="r" * 50,
            )

    assert caught.value.error_code == "FEISHU_RATE_LIMITED"
    assert caught.value.retryable is True
    assert caught.value.response_payload == {"http_status": 429}


async def test_feishu_transport_classifies_token_rejection_as_auth_failure() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 10003, "msg": "invalid app credentials"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = FeishuTransport(
            app_id="cli_test",
            app_secret="secret-test",
            client=client,
        )
        with pytest.raises(FeishuTransportError) as caught:
            await transport.send_card(
                chat_id="oc_test",
                card=FeishuCardRenderer().render(_report()),
                request_uuid="t" * 50,
            )

    assert caught.value.error_code == "FEISHU_AUTH_FAILED"
    assert len(requests) == 1


async def test_feishu_transport_rejects_an_oversized_card_before_network_io() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = FeishuTransport(
            app_id="cli_test",
            app_secret="secret-test",
            client=client,
        )
        with pytest.raises(FeishuTransportError) as caught:
            await transport.send_card(
                chat_id="oc_test",
                card={
                    "schema": "2.0",
                    "body": {"elements": [{"tag": "markdown", "content": "x" * (31 * 1024)}]},
                },
                request_uuid="s" * 50,
            )

    assert caught.value.error_code == "FEISHU_CARD_INVALID"
    assert requests == []


@pytest.mark.parametrize(
    ("status_code", "payload", "expected_error_code", "retryable"),
    [
        (
            400,
            {"code": 230027, "msg": "Lack of necessary permissions"},
            "FEISHU_AUTH_FAILED",
            False,
        ),
        (
            400,
            {"code": 230099, "msg": "failed to create card content"},
            "FEISHU_CARD_INVALID",
            False,
        ),
        (
            400,
            {"code": 230002, "msg": "The bot can not be outside the group"},
            "FEISHU_CHAT_UNAVAILABLE",
            False,
        ),
        (
            429,
            {"code": 230020, "msg": "group message rate limit"},
            "FEISHU_RATE_LIMITED",
            True,
        ),
    ],
)
async def test_feishu_transport_classifies_message_api_failures(
    status_code: int,
    payload: dict[str, Any],
    expected_error_code: str,
    retryable: bool,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        return httpx.Response(status_code, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = FeishuTransport(
            app_id="cli_test",
            app_secret="secret-test",
            client=client,
        )
        with pytest.raises(FeishuTransportError) as caught:
            await transport.send_card(
                chat_id="oc_test",
                card=FeishuCardRenderer().render(_report()),
                request_uuid="d" * 50,
            )

    assert caught.value.error_code == expected_error_code
    assert caught.value.retryable is retryable


async def test_configured_feishu_delivery_uses_the_enabled_runtime_target() -> None:
    report = _report()
    store = _Store(report)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_configured"}})

    settings = Settings(
        feishu_delivery_enabled=True,
        feishu_app_id="cli_test",
        feishu_app_secret=SecretStr("secret-test"),
        feishu_chat_id="oc_configured",
        feishu_timeout_seconds=7,
        feishu_delivery_max_attempts=2,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        delivery = ConfiguredFeishuDelivery(settings=settings, client=client, store=store)
        result = await delivery.deliver(report_id=report.report_id)

    assert result.status == "succeeded"
    assert result.delivery_target == "feishu:oc_configured"
    body = json.loads(requests[-1].content)
    assert body["receive_id"] == "oc_configured"
    assert isinstance(body["uuid"], str)
    assert len(body["uuid"]) == 50


async def test_configured_feishu_delivery_rejects_disabled_configuration() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="disabled"):
            ConfiguredFeishuDelivery(settings=Settings(), client=client, store=_Store(_report()))


def test_feishu_settings_are_required_only_when_delivery_is_enabled() -> None:
    assert Settings(_env_file=None).feishu_delivery_enabled is False
    with pytest.raises(ValueError, match="FEISHU_APP_ID"):
        Settings(_env_file=None, feishu_delivery_enabled=True)
    with pytest.raises(ValueError, match="must use HTTPS"):
        Settings(
            feishu_delivery_enabled=True,
            feishu_app_id="cli_test",
            feishu_app_secret=SecretStr("secret-test"),
            feishu_chat_id="oc_test",
            feishu_api_base_url="http://open.feishu.cn",
        )
    with pytest.raises(ValueError, match="MAX_TOTAL_ATTEMPTS"):
        Settings(
            feishu_delivery_max_attempts=3,
            feishu_delivery_max_total_attempts=2,
        )

    settings = Settings(
        feishu_delivery_enabled=True,
        feishu_app_id="cli_test",
        feishu_app_secret=SecretStr("secret-test"),
        feishu_chat_id="oc_test",
    )
    assert settings.feishu_chat_id == "oc_test"
