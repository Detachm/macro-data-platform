from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from macro_platform.contracts.common import Region
from macro_platform.contracts.market import InstrumentQuery
from macro_platform.contracts.provider import Dataset, FetchContext
from macro_platform.providers.base import (
    ProviderAuthenticationError,
    ProviderAuthorizationError,
    ProviderCursorError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderTimeoutError,
    UnsupportedCapabilityError,
)
from macro_platform.providers.cn import CN_PROVIDER_ID, CN_ROLE_BINDINGS, CnSyntheticProvider
from macro_platform.providers.hk import HK_PROVIDER_ID, HK_ROLE_BINDINGS, HkSyntheticProvider
from macro_platform.providers.registry import ProviderRegistry
from tests.contract.provider_suite import (
    CONTRACT_CASES,
    ContractCase,
    RegionalFixtureProvider,
    assert_canonical_checksum_contract,
    assert_empty_fixture_is_explicit,
    assert_error_fixture_raises,
    assert_fixture_manifest_contract,
    assert_news_normalization_contract,
    assert_success_fixture_contract,
    assert_title_only_news_contract,
)

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures"
NOW = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)
REQUEST_ID = UUID("00000000-0000-4000-8000-000000000005")


@pytest.fixture
def context() -> FetchContext:
    return FetchContext(
        request_id=REQUEST_ID,
        as_of=NOW,
        deadline_at=datetime(2026, 7, 23, 8, 1, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("manifest_path", "provider_id", "region"),
    [
        (FIXTURE_ROOT / "cn" / "manifest.json", CN_PROVIDER_ID, Region.CN),
        (FIXTURE_ROOT / "hk" / "manifest.json", HK_PROVIDER_ID, Region.HK),
    ],
)
@pytest.mark.parametrize("case", CONTRACT_CASES.values(), ids=lambda case: case.case_id)
def test_cn_hk_fixture_manifests_cover_contract_matrix(
    manifest_path: Path,
    provider_id: str,
    region: Region,
    case: ContractCase,
) -> None:
    manifest = assert_fixture_manifest_contract(manifest_path)
    assert manifest["provider_id"] == provider_id
    assert manifest["region"] == region.value
    if case.status == "xfail":
        pytest.xfail(case.xfail_reason)


@pytest.mark.parametrize("provider_cls", [CnSyntheticProvider, HkSyntheticProvider])
@pytest.mark.asyncio
async def test_success_fixtures_parse_offline_with_stable_records(
    provider_cls: type[RegionalFixtureProvider],
    context: FetchContext,
) -> None:
    await assert_success_fixture_contract(provider_cls.from_fixture("success"), context)


@pytest.mark.parametrize("provider_cls", [CnSyntheticProvider, HkSyntheticProvider])
@pytest.mark.asyncio
async def test_empty_fixture_returns_complete_empty_page(
    provider_cls: type[RegionalFixtureProvider],
    context: FetchContext,
) -> None:
    await assert_empty_fixture_is_explicit(provider_cls.from_fixture("empty"), context)


@pytest.mark.parametrize(
    ("fixture_name", "error_type"),
    [
        ("auth_failure", ProviderAuthenticationError),
        ("forbidden", ProviderAuthorizationError),
        ("rate_limited", ProviderRateLimitError),
        ("timeout", ProviderTimeoutError),
        ("missing_fields", ProviderSchemaError),
        ("schema_changed", ProviderSchemaError),
        ("html_login", ProviderAuthorizationError),
        ("duplicate_page", ProviderCursorError),
    ],
)
@pytest.mark.parametrize("provider_cls", [CnSyntheticProvider, HkSyntheticProvider])
@pytest.mark.asyncio
async def test_error_fixtures_are_not_empty_data(
    provider_cls: type[RegionalFixtureProvider],
    fixture_name: str,
    error_type: type[Exception],
    context: FetchContext,
) -> None:
    await assert_error_fixture_raises(provider_cls.from_fixture(fixture_name), context, error_type)


@pytest.mark.parametrize("provider_cls", [CnSyntheticProvider, HkSyntheticProvider])
@pytest.mark.asyncio
async def test_headline_only_news_fixture_keeps_body_empty_and_rights_explicit(
    provider_cls: type[RegionalFixtureProvider],
    context: FetchContext,
) -> None:
    await assert_title_only_news_contract(provider_cls.from_fixture("headline_only"), context)


@pytest.mark.parametrize(
    ("provider_cls", "fixture_only_datasets"),
    [
        (
            CnSyntheticProvider,
            {
                Dataset.INSTRUMENTS,
                Dataset.BARS,
                Dataset.MARKET_OBSERVATIONS,
                Dataset.MACRO_SERIES,
                Dataset.MACRO_OBSERVATIONS,
                Dataset.NEWS,
            },
        ),
        (
            HkSyntheticProvider,
            {
                Dataset.INSTRUMENTS,
                Dataset.BARS,
                Dataset.MARKET_OBSERVATIONS,
                Dataset.MACRO_SERIES,
            },
        ),
    ],
)
def test_fixture_only_sources_are_guarded_from_production_scheduling(
    provider_cls: type[RegionalFixtureProvider],
    fixture_only_datasets: set[Dataset],
) -> None:
    provider = provider_cls.from_fixture("success")
    for dataset in fixture_only_datasets:
        with pytest.raises(UnsupportedCapabilityError):
            provider.assert_production_dataset_supported(dataset)


@pytest.mark.asyncio
async def test_source_checksum_excludes_retrieved_at(tmp_path: Path, context: FetchContext) -> None:
    source_fixture = FIXTURE_ROOT / "cn" / "synthetic" / "success.json"
    changed_fixture = tmp_path / "success_changed_retrieved_at.json"
    payload = json.loads(source_fixture.read_text(encoding="utf-8"))
    payload["pages"]["instruments"]["items"][0]["retrieved_at"] = "2026-07-23T07:30:00Z"
    changed_fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    query = InstrumentQuery(regions={Region.CN})
    original = await CnSyntheticProvider.from_fixture("success").fetch_instruments(query, context)
    changed = await CnSyntheticProvider(changed_fixture).fetch_instruments(query, context)

    assert original.items[0].source.checksum_sha256 == changed.items[0].source.checksum_sha256


def test_news_normalization_and_checksum_entrypoints() -> None:
    assert_news_normalization_contract()
    assert_canonical_checksum_contract()


@pytest.mark.parametrize(
    ("provider", "role_bindings", "role"),
    [
        (
            CnSyntheticProvider.from_fixture("success"),
            CN_ROLE_BINDINGS,
            "cn.contract_fixture.macro_releases",
        ),
        (
            HkSyntheticProvider.from_fixture("success"),
            HK_ROLE_BINDINGS,
            "hk.contract_fixture.macro_releases",
        ),
    ],
)
def test_cn_hk_registry_roles_are_declared(
    provider: RegionalFixtureProvider,
    role_bindings: dict[str, str],
    role: str,
) -> None:
    registry = ProviderRegistry()
    registry.register(provider)
    for role_name, provider_id in role_bindings.items():
        registry.bind_role(role_name, provider_id)

    assert registry.resolve(role) is provider
