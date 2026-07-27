from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from macro_platform.api.app import create_app
from macro_platform.config import Settings
from macro_platform.storage.repositories import EmptyDataRepository, PostgresDataRepository

TOKEN = "test-service-token"


def client() -> TestClient:
    settings = Settings(app_env="test", service_token=SecretStr(TOKEN))
    return TestClient(create_app(settings=settings))


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_api_019_liveness_and_request_id() -> None:
    with client() as api:
        response = api.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"]


def test_rep_027_production_app_defaults_to_postgres_repository() -> None:
    settings = Settings(app_env="production", service_token=SecretStr(TOKEN))
    application = create_app(settings=settings)
    with TestClient(application):
        assert isinstance(application.state.repository, PostgresDataRepository)


def test_rep_027_production_app_rejects_empty_repository_override() -> None:
    settings = Settings(app_env="production", service_token=SecretStr(TOKEN))
    with pytest.raises(ValueError, match="PostgreSQL"):
        create_app(settings=settings, repository=EmptyDataRepository())


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
