from __future__ import annotations

from macro_platform.contracts.common import Region
from macro_platform.contracts.news import ContentMode, NewsQuery
from macro_platform.contracts.provider import Dataset
from macro_platform.governance.source_policy import (
    ApprovalStatus,
    PolicyPurpose,
    ProductionSourcePolicy,
    RetentionRule,
    SourcePolicyEntry,
    SourcePolicyManifest,
)
from macro_platform.services.news_service import NewsService
from macro_platform.storage.repositories import EmptyDataRepository
from tests.helpers import NOW, news_event, source_ref


class NewsRepository(EmptyDataRepository):
    def __init__(self, events: list) -> None:
        self.events = events

    async def list_news(self, query: NewsQuery) -> list:
        return self.events


def query(mode: ContentMode) -> NewsQuery:
    return NewsQuery(
        regions={Region.CN},
        published_from=NOW.replace(day=22),
        published_to=NOW.replace(day=24),
        as_of=NOW,
        content_mode=mode,
    )


async def test_news_017_external_llm_receives_only_authorized_content() -> None:
    allowed = news_event()
    denied = news_event(external_llm_allowed=False).model_copy(update={"news_id": "denied"})
    service = NewsService(NewsRepository([allowed, denied]))

    events = await service.events(query(ContentMode.SNIPPET), for_external_llm=True)

    assert events[0].summary is not None
    assert events[0].body is None
    assert events[1].content_mode is ContentMode.HEADLINE
    assert events[1].summary is None
    assert events[1].body is None


async def test_internal_query_preserves_original_event() -> None:
    original = news_event()
    service = NewsService(NewsRepository([original]))
    assert await service.events(query(ContentMode.SNIPPET)) == [original]


async def test_gov_026_unapproved_source_is_excluded_from_external_llm_input() -> None:
    approved = news_event().model_copy(
        update={"news_id": "approved", "source": source_ref("approved.news.v1")}
    )
    pending = news_event().model_copy(
        update={"news_id": "pending", "source": source_ref("pending.news.v1")}
    )
    policy = ProductionSourcePolicy(
        SourcePolicyManifest(
            policy_version="test",
            entries=[
                SourcePolicyEntry(
                    policy_id="approved-news",
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
                ),
                SourcePolicyEntry(
                    policy_id="pending-news",
                    provider_id="pending.news.v1",
                    dataset=Dataset.NEWS,
                    regions={Region.CN},
                    owner="@kazming666",
                    credential_requirement="none",
                    ingestion_allowed=True,
                    external_llm_allowed=True,
                    citation_allowed=True,
                    retention_rule=RetentionRule.METADATA_ONLY,
                    approval_status=ApprovalStatus.PENDING,
                    production_enabled=False,
                    evidence=["docs/data-sources/cn-hk-mvp.md"],
                ),
            ],
        )
    )
    service = NewsService(NewsRepository([approved, pending]), source_policy=policy)

    events = await service.events(query(ContentMode.SNIPPET), for_external_llm=True)

    assert [event.news_id for event in events] == ["approved"]
    assert (
        policy.decision(
            provider_id="pending.news.v1",
            dataset=Dataset.NEWS,
            region=Region.CN,
            purpose=PolicyPurpose.EXTERNAL_LLM,
        ).allowed
        is False
    )
