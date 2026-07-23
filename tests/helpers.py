from __future__ import annotations

from datetime import UTC, datetime

from macro_platform.contracts.common import AvailabilityBasis, SourceRef, UsageRights
from macro_platform.contracts.news import ContentMode, NewsEvent, SourceTier

NOW = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)
CHECKSUM = "a" * 64


def source_ref(provider_id: str = "fixture") -> SourceRef:
    return SourceRef(
        provider_id=provider_id,
        provider_record_id="record-1",
        source_name="Fixture Source",
        source_url="https://example.com/source/1",
        retrieved_at=NOW,
        checksum_sha256=CHECKSUM,
    )


def usage_rights(*, storage_allowed: bool = True, external_llm_allowed: bool = True) -> UsageRights:
    return UsageRights(
        storage_allowed=storage_allowed,
        internal_analysis_allowed=True,
        external_llm_allowed=external_llm_allowed,
        embedding_allowed=False,
        redistribution_allowed=False,
    )


def news_event(
    *,
    external_llm_allowed: bool = True,
    content_mode: ContentMode = ContentMode.SNIPPET,
    body: str | None = None,
) -> NewsEvent:
    return NewsEvent(
        news_id="news_fixture_1",
        title="央行发布政策公告",
        summary="用于合同测试的摘要",
        body=body,
        content_mode=content_mode,
        language="zh-CN",
        source_name="Fixture Source",
        source_tier=SourceTier.OFFICIAL,
        canonical_url="https://example.com/news/1",
        published_at=NOW,
        first_seen_at=NOW,
        available_at=NOW,
        availability_basis=AvailabilityBasis.FIRST_SEEN,
        regions=["CN"],
        content_hash_sha256=CHECKSUM,
        usage_rights=usage_rights(external_llm_allowed=external_llm_allowed),
        source=source_ref(),
    )
