from __future__ import annotations

import pytest

from macro_platform.contracts.common import Region
from macro_platform.contracts.provider import Dataset, ProviderCapabilities, ProviderHealth
from macro_platform.normalization.common import utc_now
from macro_platform.providers.registry import ProviderRegistry, ProviderRegistryError


class FixtureProvider:
    def __init__(self, provider_id: str, region: Region) -> None:
        self.provider_id = provider_id
        self.region = region
        self.closed = False

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            regions={self.region},
            datasets={Dataset.NEWS},
            max_page_size=100,
            supports_point_in_time=True,
            supports_revisions=False,
            supports_full_text=False,
            external_llm_allowed=True,
        )

    async def healthcheck(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.provider_id,
            status="ok",
            checked_at=utc_now(),
            latency_ms=1,
        )

    async def aclose(self) -> None:
        self.closed = True


def test_registry_resolves_role_and_sorts_capabilities() -> None:
    registry = ProviderRegistry()
    provider_b = FixtureProvider("z-provider", Region.US)
    provider_a = FixtureProvider("a-provider", Region.CN)
    registry.register(provider_b)
    registry.register(provider_a)
    registry.bind_role("cn.news.primary", "a-provider")

    assert registry.resolve("cn.news.primary") is provider_a
    assert [item.provider_id for item in registry.capabilities()] == ["a-provider", "z-provider"]


def test_registry_rejects_duplicate_provider_id() -> None:
    registry = ProviderRegistry()
    registry.register(FixtureProvider("same-provider", Region.CN))
    with pytest.raises(ProviderRegistryError, match="already registered"):
        registry.register(FixtureProvider("same-provider", Region.HK))


def test_registry_rejects_unknown_bindings_and_roles() -> None:
    registry = ProviderRegistry()
    with pytest.raises(ProviderRegistryError, match="unknown provider"):
        registry.bind_role("cn.news.primary", "missing")
    with pytest.raises(ProviderRegistryError, match="not bound"):
        registry.resolve("cn.news.primary")


@pytest.mark.asyncio
async def test_registry_closes_all_providers() -> None:
    registry = ProviderRegistry()
    provider = FixtureProvider("fixture-provider", Region.CN)
    registry.register(provider)
    await registry.close()
    assert provider.closed is True
