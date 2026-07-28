from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from macro_platform.api.app import create_app
from macro_platform.config import Settings
from macro_platform.contracts.provider import Dataset
from macro_platform.services.delivery_recovery import DeliveryRecoveryResult
from macro_platform.services.workflow_operations import (
    DailyWorkflowOperationsStatus,
    WorkerReadinessStatus,
    WorkflowDeliveryView,
)
from macro_platform.storage.repositories import EmptyDataRepository, PostgresDataRepository

TOKEN = "test-service-token"
NOW = datetime(2026, 7, 28, 0, 30, tzinfo=UTC)


def client() -> TestClient:
    settings = Settings(app_env="test", service_token=SecretStr(TOKEN))
    return TestClient(create_app(settings=settings))


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


class _WorkflowOperations:
    def __init__(self) -> None:
        self.report_dates: list[date] = []

    async def load(self, report_date: date) -> DailyWorkflowOperationsStatus:
        self.report_dates.append(report_date)
        return DailyWorkflowOperationsStatus(
            report_date=report_date,
            status="delivered",
            operator_attention_required=False,
            deliveries=(
                WorkflowDeliveryView(
                    delivery_id=uuid4(),
                    report_id=f"daily-report-{report_date.isoformat()}-v1",
                    report_version="v1",
                    status="succeeded",
                    attempt_no=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ),
            last_updated_at=NOW,
        )


class _WorkerReadiness:
    def __init__(self, *, ready: bool) -> None:
        self.ready = ready

    async def check(self) -> WorkerReadinessStatus:
        return WorkerReadinessStatus(
            status="ready" if self.ready else "not_ready",
            database_ready=self.ready,
            schema_ready=self.ready,
            provider_mode_live=self.ready,
            us_provider_mode_live=self.ready,
            us_credentials_configured=self.ready,
            feishu_delivery_enabled=self.ready,
            feishu_credentials_configured=self.ready,
            daily_chat_configured=self.ready,
            alert_chat_configured=self.ready,
            chats_are_distinct=self.ready,
            unmet_requirements=() if self.ready else ("DATABASE_UNAVAILABLE",),
        )


class _DeliveryRecovery:
    def __init__(self, *, status: str) -> None:
        self.status = status
        self.calls: list[tuple[UUID, str, bool]] = []

    async def retry(
        self,
        *,
        request_id: UUID,
        report_id: str,
        confirmed_not_delivered: bool,
    ) -> DeliveryRecoveryResult:
        self.calls.append((request_id, report_id, confirmed_not_delivered))
        return DeliveryRecoveryResult(
            action_id=uuid4(),
            request_id=request_id,
            report_id=report_id,
            status=self.status,  # type: ignore[arg-type]
            delivery_status="succeeded" if self.status == "succeeded" else "failed",
            error_code=None if self.status == "succeeded" else "FEISHU_SEND_FAILED",
        )


def test_api_019_liveness_and_request_id() -> None:
    with client() as api:
        response = api.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"]


def test_e2e_033_workflow_operations_are_protected_and_sanitized() -> None:
    settings = Settings(app_env="test", service_token=SecretStr(TOKEN))
    operations = _WorkflowOperations()
    with TestClient(create_app(settings=settings, workflow_operations=operations)) as api:
        unauthenticated = api.get("/v1/operations/daily-workflows/2026-07-28")
        response = api.get(
            "/v1/operations/daily-workflows/2026-07-28",
            headers=auth(),
        )

    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    assert operations.report_dates == [date(2026, 7, 28)]
    payload = response.json()
    assert payload["data"]["status"] == "delivered"
    assert payload["data"]["deliveries"][0]["status"] == "succeeded"
    serialized = response.text.lower()
    assert "delivery_target" not in serialized
    assert "request_payload" not in serialized
    assert "response_payload" not in serialized
    assert "message_id" not in serialized


@pytest.mark.parametrize(
    ("ready", "expected_status"),
    [(True, 200), (False, 503)],
)
def test_e2e_033_worker_readiness_has_an_explicit_http_status(
    ready: bool,
    expected_status: int,
) -> None:
    settings = Settings(app_env="test", service_token=SecretStr(TOKEN))
    with TestClient(
        create_app(
            settings=settings,
            worker_readiness=_WorkerReadiness(ready=ready),
        )
    ) as api:
        response = api.get("/v1/operations/worker-readiness", headers=auth())

    assert response.status_code == expected_status
    assert response.json()["data"]["status"] == ("ready" if ready else "not_ready")


@pytest.mark.parametrize(
    ("recovery_status", "expected_http_status"),
    [
        ("succeeded", 200),
        ("pending", 202),
        ("rejected", 409),
        ("failed", 503),
    ],
)
def test_e2e_033_delivery_recovery_is_protected_and_passes_request_id(
    recovery_status: str,
    expected_http_status: int,
) -> None:
    settings = Settings(app_env="test", service_token=SecretStr(TOKEN))
    recovery = _DeliveryRecovery(status=recovery_status)
    request_id = uuid4()
    with TestClient(create_app(settings=settings, delivery_recovery=recovery)) as api:
        unauthenticated = api.post(
            "/v1/operations/daily-reports/daily-report-2026-07-28-v1/delivery-retry",
            json={"confirmed_not_delivered": True},
        )
        response = api.post(
            "/v1/operations/daily-reports/daily-report-2026-07-28-v1/delivery-retry",
            headers={**auth(), "X-Request-ID": str(request_id)},
            json={"confirmed_not_delivered": True},
        )

    assert unauthenticated.status_code == 401
    assert response.status_code == expected_http_status
    assert recovery.calls == [
        (request_id, "daily-report-2026-07-28-v1", True),
    ]
    assert response.json()["data"]["request_id"] == str(request_id)


def test_e2e_033_delivery_recovery_fails_closed_when_not_configured() -> None:
    with client() as api:
        response = api.post(
            "/v1/operations/daily-reports/daily-report-2026-07-28-v1/delivery-retry",
            headers=auth(),
            json={"confirmed_not_delivered": False},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DELIVERY_RECOVERY_NOT_CONFIGURED"


def test_e2e_033_delivery_recovery_requires_a_client_request_id() -> None:
    settings = Settings(app_env="test", service_token=SecretStr(TOKEN))
    recovery = _DeliveryRecovery(status="succeeded")
    with TestClient(create_app(settings=settings, delivery_recovery=recovery)) as api:
        response = api.post(
            "/v1/operations/daily-reports/daily-report-2026-07-28-v1/delivery-retry",
            headers=auth(),
            json={"confirmed_not_delivered": False},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_REQUEST_ID_REQUIRED"
    assert recovery.calls == []


def test_rep_027_production_app_defaults_to_postgres_repository() -> None:
    settings = Settings(
        app_env="production",
        provider_mode="live",
        provider_cursor_secret=SecretStr("test-provider-cursor-secret"),
        service_token=SecretStr(TOKEN),
        us_provider_mode="live",
        twelve_data_api_key=SecretStr("test-twelve-data-key"),
        twelve_data_cursor_secret=SecretStr("test-twelve-data-cursor"),
    )
    application = create_app(settings=settings)
    with TestClient(application):
        assert isinstance(application.state.repository, PostgresDataRepository)
        assert (
            application.state.provider_registry.resolve("us.market.primary")
            .capabilities()
            .provider_id
            == "us.twelve-data.v1"
        )
        application.state.provider_registry.resolve(
            "us.market.primary"
        ).assert_production_dataset_supported(Dataset.BARS)


def test_rep_027_production_app_rejects_empty_repository_override() -> None:
    settings = Settings(
        app_env="production",
        provider_mode="live",
        provider_cursor_secret=SecretStr("test-provider-cursor-secret"),
        service_token=SecretStr(TOKEN),
    )
    with pytest.raises(ValueError, match="PostgreSQL"):
        create_app(settings=settings, repository=EmptyDataRepository())


def test_PRV_001_production_app_rejects_fixture_us_provider_mode() -> None:
    with pytest.raises(ValueError, match="PROVIDER_MODE=live"):
        create_app(settings=Settings(app_env="production", service_token=SecretStr(TOKEN)))

    settings = Settings(
        app_env="production",
        provider_mode="live",
        provider_cursor_secret=SecretStr("test-provider-cursor-secret"),
        service_token=SecretStr(TOKEN),
    )
    with pytest.raises(ValueError, match="provider mode must be live"):
        create_app(settings=settings)


def test_api_008_protected_route_requires_bearer_token() -> None:
    with client() as api:
        response = api.get("/v1/meta/capabilities")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_capabilities_uses_stable_success_envelope() -> None:
    with client() as api:
        response = api.get("/v1/meta/capabilities", headers=auth())
    payload = response.json()
    assert response.status_code == 200
    assert payload["api_version"] == "v1"
    assert payload["data"] == {"items": []}
    assert payload["request_id"] == response.headers["X-Request-ID"]


def test_api_023_editor_context_is_available_with_explicit_coverage_gaps() -> None:
    with client() as api:
        response = api.post(
            "/v1/editor/context",
            headers=auth(),
            json={"regions": ["CN"], "as_of": "2026-07-23T08:00:00Z"},
        )
    payload = response.json()["data"]
    assert response.status_code == 200
    assert payload["context_version"] == "1.0"
    assert payload["market_snapshots"] == []
    assert [(item["dataset"], item["status"]) for item in payload["coverage"]] == [
        ("market", "unavailable"),
        ("macro", "unavailable"),
        ("news", "unavailable"),
    ]


def test_api_015_editor_context_can_fail_closed_on_missing_data() -> None:
    with client() as api:
        response = api.post(
            "/v1/editor/context",
            headers=auth(),
            json={
                "regions": ["CN"],
                "as_of": "2026-07-23T08:00:00Z",
                "fail_on_incomplete": True,
            },
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATASET_UNAVAILABLE"


def test_api_018_editor_context_rejects_unknown_request_fields() -> None:
    with client() as api:
        response = api.post(
            "/v1/editor/context",
            headers=auth(),
            json={"regions": ["CN"], "unknown": True},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_empty_repository_data_routes_share_the_same_list_envelope() -> None:
    cases: list[tuple[str, list[tuple[str, str]]]] = [
        ("/v1/instruments", [("regions", "CN")]),
        (
            "/v1/market/bars",
            [
                ("instrument_id", "ins_fixture_1"),
                ("interval", "1d"),
                ("start", "2026-07-22T00:00:00Z"),
                ("end", "2026-07-23T00:00:00Z"),
            ],
        ),
        ("/v1/market/snapshots", [("instrument_id", "ins_fixture_1")]),
        (
            "/v1/market/observations",
            [
                ("regions", "CN"),
                ("metric_code", "flow.northbound.net_buy"),
                ("start", "2026-07-22T00:00:00Z"),
                ("end", "2026-07-23T00:00:00Z"),
            ],
        ),
        ("/v1/macro/series", [("regions", "CN")]),
        (
            "/v1/macro/observations",
            [
                ("series_id", "macro:CN:NBS:CPI_YOY"),
                ("period_from", "2026-01-01"),
                ("period_to", "2026-07-23"),
            ],
        ),
        (
            "/v1/macro/releases",
            [
                ("regions", "CN"),
                ("scheduled_from", "2026-07-22T00:00:00Z"),
                ("scheduled_to", "2026-07-24T00:00:00Z"),
            ],
        ),
        (
            "/v1/news",
            [
                ("regions", "CN"),
                ("published_from", "2026-07-22T00:00:00Z"),
                ("published_to", "2026-07-24T00:00:00Z"),
            ],
        ),
    ]

    with client() as api:
        responses = [api.get(path, headers=auth(), params=params) for path, params in cases]

    assert [response.status_code for response in responses] == [200] * len(cases)
    assert all(response.json()["data"] == {"items": []} for response in responses)
