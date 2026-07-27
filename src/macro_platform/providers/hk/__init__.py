from __future__ import annotations

from datetime import date
from pathlib import Path

from macro_platform.contracts.common import Region
from macro_platform.contracts.provider import Dataset
from macro_platform.providers._regional_fixture import RegionalFixtureProvider
from macro_platform.providers.hk.live import (
    HkCsdProvider,
    HkLiveMacroProvider,
    HkmaLiveNewsProvider,
    HkmaPressReleaseProvider,
)
from macro_platform.providers.hk.xtquant import (
    HK_XTQUANT_DEFAULT_INSTRUMENTS,
    HK_XTQUANT_PRIMARY_ROLE,
    HK_XTQUANT_PROVIDER_ID,
    HkXtQuantDailyBarsProvider,
    HkXtQuantInstrument,
    hk_xtquant_instruments_from_symbols,
    register_hk_xtquant_provider_roles,
)
from macro_platform.providers.registry import ProviderRegistry

HK_PROVIDER_ID = "hk.contract-fixture.v1"
HK_ROLE_BINDINGS = {
    "hk.contract_fixture.instruments": HK_PROVIDER_ID,
    "hk.contract_fixture.bars": HK_PROVIDER_ID,
    "hk.contract_fixture.market_observations": HK_PROVIDER_ID,
    "hk.contract_fixture.macro_series": HK_PROVIDER_ID,
    "hk.contract_fixture.macro_observations": HK_PROVIDER_ID,
    "hk.contract_fixture.macro_releases": HK_PROVIDER_ID,
    "hk.contract_fixture.news": HK_PROVIDER_ID,
}


class HkSyntheticProvider(RegionalFixtureProvider):
    provider_id = HK_PROVIDER_ID
    region = Region.HK
    source_name = "HK Contract Fixture Provider"
    fixture_dir = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "hk" / "synthetic"
    macro_authority = "CENSTATD"
    macro_code = "510-60004"
    macro_series_name = "HK CPI YoY"
    instrument_listed_on_by_symbol = {"XHKG:00700": date(2004, 6, 16)}
    instrument_key_by_symbol = {"XHKG:00700": "hk-security-tencent-holdings"}
    live_ready_datasets = frozenset()
    live_candidate_datasets = frozenset(
        {Dataset.MACRO_OBSERVATIONS, Dataset.MACRO_RELEASES, Dataset.NEWS}
    )
    fixture_only_datasets = frozenset(
        {
            Dataset.INSTRUMENTS,
            Dataset.BARS,
            Dataset.MARKET_OBSERVATIONS,
            Dataset.MACRO_SERIES,
            Dataset.MACRO_OBSERVATIONS,
            Dataset.MACRO_RELEASES,
            Dataset.NEWS,
        }
    )


HkContractFixtureProvider = HkSyntheticProvider


def register_hk_provider_roles(registry: ProviderRegistry, provider: HkSyntheticProvider) -> None:
    registry.register(provider)
    for role, provider_id in HK_ROLE_BINDINGS.items():
        registry.bind_role(role, provider_id)


__all__ = [
    "HK_PROVIDER_ID",
    "HK_ROLE_BINDINGS",
    "HK_XTQUANT_DEFAULT_INSTRUMENTS",
    "HK_XTQUANT_PRIMARY_ROLE",
    "HK_XTQUANT_PROVIDER_ID",
    "HkContractFixtureProvider",
    "HkSyntheticProvider",
    "HkXtQuantDailyBarsProvider",
    "HkXtQuantInstrument",
    "HkCsdProvider",
    "HkLiveMacroProvider",
    "HkmaLiveNewsProvider",
    "HkmaPressReleaseProvider",
    "hk_xtquant_instruments_from_symbols",
    "register_hk_provider_roles",
    "register_hk_xtquant_provider_roles",
]
