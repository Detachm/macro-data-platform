from __future__ import annotations

from macro_platform.config import Settings
from macro_platform.contracts.provider import Dataset
from macro_platform.providers.cn import (
    BaoStockDailyBarsProvider,
    CnSyntheticProvider,
    register_cn_baostock_provider_roles,
    register_cn_provider_roles,
)
from macro_platform.providers.cn.live import CnNbsNewsProvider, CnNbsReleaseProvider
from macro_platform.providers.hk import (
    HK_CENSTATD_CALENDAR_ROLE,
    HkSyntheticProvider,
    HkXtQuantDailyBarsProvider,
    hk_xtquant_instruments_from_symbols,
    register_hk_provider_roles,
    register_hk_xtquant_provider_roles,
)
from macro_platform.providers.hk.live import HkCsdProvider, HkmaPressReleaseProvider
from macro_platform.providers.hk.release_calendar import HkCenstatdReleaseCalendarProvider
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
        cn_news = CnNbsNewsProvider(
            timeout_seconds=settings.provider_timeout_seconds,
            cursor_signing_secret=cursor_secret,
        )
        cn_baostock = BaoStockDailyBarsProvider(
            timeout_seconds=settings.provider_timeout_seconds,
            cursor_signing_secret=cursor_secret,
        )
        hk_xtquant = HkXtQuantDailyBarsProvider(
            instruments=hk_xtquant_instruments_from_symbols(settings.hk_xtquant_symbols),
            host=settings.hk_xtquant_host,
            port=settings.hk_xtquant_port,
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
        hk_calendar = HkCenstatdReleaseCalendarProvider(
            timeout_seconds=settings.provider_timeout_seconds,
            cursor_signing_secret=cursor_secret,
        )
        registry.register(cn)
        registry.register(cn_news)
        register_cn_baostock_provider_roles(registry, cn_baostock)
        register_hk_xtquant_provider_roles(registry, hk_xtquant)
        registry.register(hk_csd)
        registry.register(hkma)
        registry.register(hk_calendar)
        registry.bind_role("cn.macro.primary", cn.provider_id)
        registry.bind_role("cn.news.primary", cn_news.provider_id)
        registry.bind_role("hk.macro.primary", hk_csd.provider_id)
        registry.bind_role("hk.news.primary", hkma.provider_id)
        registry.bind_role(
            HK_CENSTATD_CALENDAR_ROLE,
            hk_calendar.provider_id,
            required_dataset=Dataset.MACRO_RELEASES,
        )

    if settings.app_env == "production":
        create_us_provider_registry_from_settings(settings, registry=registry)
    return registry


__all__ = ["create_provider_registry"]
