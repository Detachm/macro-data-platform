from __future__ import annotations

from datetime import date
from pathlib import Path

from macro_platform.contracts.common import Region
from macro_platform.contracts.provider import Dataset
from macro_platform.providers._regional_fixture import RegionalFixtureProvider
from macro_platform.providers.registry import ProviderRegistry

CN_PROVIDER_ID = "cn.contract-fixture.v1"
CN_ROLE_BINDINGS = {
    "cn.instruments.primary": CN_PROVIDER_ID,
    "cn.bars.primary": CN_PROVIDER_ID,
    "cn.macro.primary": CN_PROVIDER_ID,
    "cn.news.primary": CN_PROVIDER_ID,
    "cn.contract_fixture.instruments": CN_PROVIDER_ID,
    "cn.contract_fixture.bars": CN_PROVIDER_ID,
    "cn.contract_fixture.market_observations": CN_PROVIDER_ID,
    "cn.contract_fixture.macro_series": CN_PROVIDER_ID,
    "cn.contract_fixture.macro_observations": CN_PROVIDER_ID,
    "cn.contract_fixture.macro_releases": CN_PROVIDER_ID,
    "cn.contract_fixture.news": CN_PROVIDER_ID,
}


class CnSyntheticProvider(RegionalFixtureProvider):
    provider_id = CN_PROVIDER_ID
    region = Region.CN
    source_name = "CN Contract Fixture Provider"
    fixture_dir = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "cn" / "synthetic"
    macro_authority = "NBS"
    macro_code = "CPI_YOY"
    macro_series_name = "CN CPI YoY"
    instrument_listed_on_by_symbol = {"XSHG:600000": date(1999, 11, 10)}
    instrument_key_by_symbol = {"XSHG:600000": "cn-security-shanghai-pudong-development-bank"}
    live_ready_datasets = frozenset()
    live_candidate_datasets = frozenset({Dataset.MACRO_RELEASES})
    fixture_only_datasets = frozenset(
        {
            Dataset.INSTRUMENTS,
            Dataset.BARS,
            Dataset.MARKET_OBSERVATIONS,
            Dataset.MACRO_SERIES,
            Dataset.MACRO_OBSERVATIONS,
            Dataset.NEWS,
        }
    )


CnContractFixtureProvider = CnSyntheticProvider


def register_cn_provider_roles(registry: ProviderRegistry, provider: CnSyntheticProvider) -> None:
    registry.register(provider)
    for role, provider_id in CN_ROLE_BINDINGS.items():
        registry.bind_role(role, provider_id)


__all__ = [
    "CN_PROVIDER_ID",
    "CN_ROLE_BINDINGS",
    "CnContractFixtureProvider",
    "CnSyntheticProvider",
    "register_cn_provider_roles",
]
