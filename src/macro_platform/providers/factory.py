from __future__ import annotations

from macro_platform.config import Settings
from macro_platform.providers.cn import CnSyntheticProvider, register_cn_provider_roles
from macro_platform.providers.cn.live import CnNbsReleaseProvider
from macro_platform.providers.hk import HkSyntheticProvider, register_hk_provider_roles
from macro_platform.providers.hk.live import HkCsdProvider, HkmaPressReleaseProvider
from macro_platform.providers.registry import ProviderRegistry
from macro_platform.providers.us.factory import create_us_provider_registry_from_settings


def create_provider_registry(settings: Settings) -> ProviderRegistry:
    """Build an explicitly selected provider graph.

    Fixture providers are bound only to ``*.contract_fixture`` roles. Live
    providers are bound only to production roles and are limited by each
    provider's advertised capabilities.
    """

    registry = ProviderRegistry()
    if settings.provider_mode == "fixture":
        register_cn_provider_roles(
            registry, CnSyntheticProvider(CnSyntheticProvider.fixture_dir / "success.json")
        )
        register_hk_provider_roles(
            registry, HkSyntheticProvider(HkSyntheticProvider.fixture_dir / "success.json")
        )
    else:
        cursor_secret = settings.provider_cursor_secret.get_secret_value()
        cn = CnNbsReleaseProvider(
            timeout_seconds=settings.provider_timeout_seconds,
            cursor_signing_secret=cursor_secret,
        )
        hk_csd = HkCsdProvider(
            timeout_seconds=settings.provider_timeout_seconds,
            cursor_signing_secret=cursor_secret,
        )
        hkma = HkmaPressReleaseProvider(
            timeout_seconds=settings.provider_timeout_seconds,
            cursor_signing_secret=cursor_secret,
        )
        registry.register(cn)
        registry.register(hk_csd)
        registry.register(hkma)
        registry.bind_role("cn.macro.primary", cn.provider_id)
        registry.bind_role("hk.macro.primary", hk_csd.provider_id)
        registry.bind_role("hk.news.primary", hkma.provider_id)

    if settings.app_env == "production":
        create_us_provider_registry_from_settings(settings, registry=registry)
    return registry


__all__ = ["create_provider_registry"]
