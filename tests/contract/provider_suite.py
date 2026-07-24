from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from macro_platform.contracts.common import StrictModel
from macro_platform.contracts.news import ContentMode, NewsEvent
from macro_platform.contracts.provider import ProviderCapabilities, ProviderPage
from macro_platform.providers.base import BaseProvider


def assert_capabilities_contract(provider: BaseProvider) -> ProviderCapabilities:
    capabilities = provider.capabilities()
    assert capabilities.provider_id.strip() == capabilities.provider_id
    assert capabilities.regions
    assert capabilities.datasets
    assert capabilities.max_page_size > 0
    return capabilities


def assert_page_contract(page: ProviderPage[StrictModel]) -> None:
    assert page.fetched_at.tzinfo is not None
    if page.next_cursor is not None:
        assert page.next_cursor
        assert page.next_cursor.strip() == page.next_cursor
    for item in page.items:
        assert isinstance(item, StrictModel)


def assert_stable_page(first: ProviderPage[StrictModel], second: ProviderPage[StrictModel]) -> None:
    assert _canonical_items(first.items) == _canonical_items(second.items)


def assert_provenance_contract(page: ProviderPage[StrictModel]) -> None:
    for item in page.items:
        source = item.model_dump().get("source")
        assert isinstance(source, dict)
        assert isinstance(source.get("provider_id"), str)
        assert source["provider_id"].strip() == source["provider_id"]
        assert isinstance(source.get("provider_record_id"), str)
        assert source["provider_record_id"].strip() == source["provider_record_id"]
        assert isinstance(source.get("source_name"), str)
        assert source["source_name"].strip() == source["source_name"]
        source_url = source.get("source_url")
        assert source_url is None or str(source_url).strip() == str(source_url)
        assert isinstance(source.get("checksum_sha256"), str)
        assert len(source["checksum_sha256"]) == 64
        assert all(character in "0123456789abcdef" for character in source["checksum_sha256"])
        assert isinstance(source.get("retrieved_at"), datetime)


def assert_pit_contract(page: ProviderPage[StrictModel], *, as_of: datetime) -> None:
    for item in page.items:
        available_at = item.model_dump().get("available_at")
        assert isinstance(available_at, datetime)
        assert available_at <= as_of


def assert_news_contract(page: ProviderPage[NewsEvent]) -> None:
    for event in page.items:
        assert event.content_hash_sha256
        assert isinstance(event.usage_rights.storage_allowed, bool)
        assert isinstance(event.usage_rights.external_llm_allowed, bool)
        if event.body is not None:
            assert event.content_mode is ContentMode.FULL_TEXT
            assert event.usage_rights.storage_allowed


def _canonical_items(items: Sequence[StrictModel]) -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in items]
