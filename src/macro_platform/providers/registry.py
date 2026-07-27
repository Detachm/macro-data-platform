from __future__ import annotations

from macro_platform.contracts.provider import Dataset, ProviderCapabilities
from macro_platform.providers.base import BaseProvider


class ProviderRegistryError(RuntimeError):
    pass


class ProviderRegistry:
    """Holds provider instances and maps stable logical roles to them."""

    def __init__(self) -> None:
        self._providers: dict[str, BaseProvider] = {}
        self._roles: dict[str, str] = {}

    def register(self, provider: BaseProvider) -> None:
        provider_id = provider.capabilities().provider_id
        if provider_id in self._providers:
            raise ProviderRegistryError(f"provider already registered: {provider_id}")
        self._providers[provider_id] = provider

    def bind_role(
        self, role: str, provider_id: str, *, required_dataset: Dataset | None = None
    ) -> None:
        if provider_id not in self._providers:
            raise ProviderRegistryError(f"unknown provider: {provider_id}")
        capabilities = self._providers[provider_id].capabilities()
        if required_dataset is not None and required_dataset not in capabilities.datasets:
            raise ProviderRegistryError(
                f"provider {provider_id} does not advertise dataset {required_dataset.value}"
            )
        self._roles[role] = provider_id

    def resolve(self, role: str) -> BaseProvider:
        provider_id = self._roles.get(role)
        if provider_id is None:
            raise ProviderRegistryError(f"provider role is not bound: {role}")
        return self._providers[provider_id]

    def capabilities(self) -> list[ProviderCapabilities]:
        return [
            provider.capabilities()
            for _, provider in sorted(self._providers.items(), key=lambda item: item[0])
        ]

    def assert_production_safe(self) -> None:
        """Reject fixture roles before a production app can start."""

        for role, provider_id in self._roles.items():
            if "fixture" in role.lower() or "fixture" in provider_id.lower():
                raise ProviderRegistryError(
                    f"fixture provider role is not allowed in production: {role}"
                )

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.aclose()
