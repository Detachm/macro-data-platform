from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import SecretStr

from macro_platform.api.app import create_app
from macro_platform.config import Settings
from macro_platform.contracts.common import Region
from macro_platform.contracts.news import NewsEvent, NewsQuery
from macro_platform.contracts.provider import Dataset
from macro_platform.governance.source_policy import (
    ApprovalStatus,
    ProductionSourcePolicy,
    RetentionRule,
    SourcePolicyEntry,
    SourcePolicyManifest,
)
from macro_platform.storage.repositories import EmptyDataRepository
from tests.helpers import NOW, news_event, source_ref

TOKEN = "production-policy-token"


class _NewsRepository(EmptyDataRepository):
    def __init__(self, events: list[NewsEvent]) -> None:
        self._events = events

    async def list_news(self, query: NewsQuery) -> list[NewsEvent]:
        return self._events


def _policy() -> ProductionSourcePolicy:
    allowed = SourcePolicyEntry(
        policy_id="test.approved-news",
        provider_id="approved.news.v1",
        dataset=Dataset.NEWS,
        regions={Region.CN},
        owner="@kazming666",
        credential_requirement="none",
        ingestion_allowed=True,
        external_llm_allowed=True,
        citation_allowed=True,
        retention_rule=RetentionRule.METADATA_ONLY,
        approval_status=ApprovalStatus.APPROVED,
        production_enabled=True,
        evidence=["docs/data-sources/cn-hk-mvp.md"],
    )
    pending = allowed.model_copy(
        update={
            "policy_id": "test.pending-news",
            "provider_id": "pending.news.v1",
            "approval_status": ApprovalStatus.PENDING,
            "production_enabled": False,
        }
    )
    return ProductionSourcePolicy(
        SourcePolicyManifest(policy_version="test", entries=[allowed, pending])
    )


def test_gov_026_production_editor_context_excludes_pending_and_missing_sources() -> None:
    approved = news_event().model_copy(
        update={"news_id": "approved", "source": source_ref("approved.news.v1")}
    )
    pending = news_event().model_copy(
        update={"news_id": "pending", "source": source_ref("pending.news.v1")}
    )
    missing = news_event().model_copy(
        update={"news_id": "missing", "source": source_ref("missing.news.v1")}
    )
    app = create_app(
        settings=Settings(app_env="production", service_token=SecretStr(TOKEN)),
        repository=_NewsRepository([approved, pending, missing]),
        source_policy=_policy(),
    )

    with TestClient(app) as api:
        response = api.post(
            "/v1/editor/context",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"regions": ["CN"], "as_of": NOW.isoformat()},
        )

    assert response.status_code == 200
    assert [event["news_id"] for event in response.json()["data"]["news_events"]] == ["approved"]
