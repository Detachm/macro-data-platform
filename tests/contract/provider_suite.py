from __future__ import annotations

from collections.abc import Sequence

from macro_platform.contracts.common import StrictModel
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


def _canonical_items(items: Sequence[StrictModel]) -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in items]
