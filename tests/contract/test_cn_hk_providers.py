from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from macro_platform.contracts.common import Region
from macro_platform.contracts.provider import Dataset, FetchContext
from macro_platform.providers.base import (
    ProviderAuthenticationError,
    ProviderAuthorizationError,
    ProviderCursorError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderTimeoutError,
)
from macro_platform.providers.cn import CN_PROVIDER_ID, CN_ROLE_BINDINGS, CnSyntheticProvider
from macro_platform.providers.hk import HK_PROVIDER_ID, HK_ROLE_BINDINGS, HkSyntheticProvider
from tests.contract.provider_suite import (
    CONTRACT_CASES,
    ContractCase,
    RegionalFixtureProvider,
    assert_canonical_checksum_contract,
    assert_empty_fixture_is_explicit,
    assert_error_fixture_raises,
    assert_fixture_manifest_case_contract,
    assert_fixture_only_health_contract,
    assert_fixture_only_scheduling_contract,
    assert_full_text_storage_rights_contract,
    assert_news_identity_contract,
    assert_news_normalization_contract,
    assert_registry_role_contract,
    assert_restricted_news_editor_context_contract,
    assert_source_checksum_excludes_retrieved_at_contract,
    assert_success_fixture_contract,
    assert_title_fallback_news_identity_contract,
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
    assert_fixture_manifest_case_contract(manifest_path, provider_id, region.value, case)


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
        ("html_login", ProviderSchemaError),
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
    ("provider_cls", "source_fixture"),
    [
        (CnSyntheticProvider, FIXTURE_ROOT / "cn" / "synthetic" / "success.json"),
        (HkSyntheticProvider, FIXTURE_ROOT / "hk" / "synthetic" / "success.json"),
    ],
)
@pytest.mark.asyncio
async def test_news_002_003_provider_identity_uses_canonical_url_title_and_entities(
    tmp_path: Path,
    context: FetchContext,
    provider_cls: type[RegionalFixtureProvider],
    source_fixture: Path,
) -> None:
    await assert_news_identity_contract(provider_cls, source_fixture, tmp_path, context)


@pytest.mark.parametrize(
    ("provider_cls", "source_fixture"),
    [
        (CnSyntheticProvider, FIXTURE_ROOT / "cn" / "synthetic" / "success.json"),
        (HkSyntheticProvider, FIXTURE_ROOT / "hk" / "synthetic" / "success.json"),
    ],
)
@pytest.mark.asyncio
async def test_news_003_title_fallback_is_shared_by_cn_and_hk(
    tmp_path: Path,
    context: FetchContext,
    provider_cls: type[RegionalFixtureProvider],
    source_fixture: Path,
) -> None:
    await assert_title_fallback_news_identity_contract(
        provider_cls,
        source_fixture,
        tmp_path,
        context,
    )


@pytest.mark.parametrize("provider_cls", [CnSyntheticProvider, HkSyntheticProvider])
@pytest.mark.asyncio
async def test_news_017_editor_context_omits_restricted_summary_and_body(
    context: FetchContext,
    provider_cls: type[RegionalFixtureProvider],
) -> None:
    await assert_restricted_news_editor_context_contract(provider_cls, context)


@pytest.mark.asyncio
async def test_full_text_news_requires_all_body_usage_rights(
    tmp_path: Path,
    context: FetchContext,
) -> None:
    source_fixture = FIXTURE_ROOT / "cn" / "synthetic" / "success.json"
    restricted_fixture = tmp_path / "full_text_without_external_rights.json"
    payload = json.loads(source_fixture.read_text(encoding="utf-8"))
    news = payload["pages"]["news"]["items"][0]
    news["body"] = "Synthetic full text that must not be retained."
    news["content_mode"] = "full_text"
    news["rights"] = {
        **news["rights"],
        "external_llm_allowed": False,
        "embedding_allowed": False,
    }
    restricted_fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    page = await CnSyntheticProvider(restricted_fixture).fetch_news(
        NewsQuery(
            regions={Region.CN},
            published_from=datetime(2026, 7, 22, tzinfo=UTC),
            published_to=datetime(2026, 7, 24, tzinfo=UTC),
            as_of=context.as_of,
            content_mode=ContentMode.SNIPPET,
        ),
        context,
    )

    assert page.items[0].body is None
    assert page.items[0].content_mode is ContentMode.SNIPPET
    assert "body_omitted_by_rights" in page.items[0].quality_flags


@pytest.mark.parametrize("provider_cls", [CnSyntheticProvider, HkSyntheticProvider])
@pytest.mark.asyncio
async def test_fixture_only_provider_health_is_not_configured(
    provider_cls: type[RegionalFixtureProvider],
) -> None:
    health = await provider_cls.from_fixture("success").healthcheck()

    assert health.status == "not_configured"
    assert health.message is not None
    assert health.message.startswith("fixture-only provider:")


@pytest.mark.parametrize(
    ("provider_cls", "source_fixture"),
    [
        (CnSyntheticProvider, FIXTURE_ROOT / "cn" / "synthetic" / "success.json"),
        (HkSyntheticProvider, FIXTURE_ROOT / "hk" / "synthetic" / "success.json"),
    ],
)
@pytest.mark.asyncio
async def test_full_text_news_can_be_saved_without_external_or_embedding_rights(
    tmp_path: Path,
    context: FetchContext,
    provider_cls: type[RegionalFixtureProvider],
    source_fixture: Path,
) -> None:
    await assert_full_text_storage_rights_contract(
        provider_cls,
        source_fixture,
        tmp_path,
        context,
    )


@pytest.mark.parametrize("provider_cls", [CnSyntheticProvider, HkSyntheticProvider])
@pytest.mark.asyncio
async def test_fixture_only_provider_health_is_not_configured(
    provider_cls: type[RegionalFixtureProvider],
) -> None:
    await assert_fixture_only_health_contract(provider_cls.from_fixture("success"))


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
    assert_fixture_only_scheduling_contract(
        provider_cls.from_fixture("success"), fixture_only_datasets
    )


@pytest.mark.parametrize(
    ("provider_cls", "source_fixture"),
    [
        (CnSyntheticProvider, FIXTURE_ROOT / "cn" / "synthetic" / "success.json"),
        (HkSyntheticProvider, FIXTURE_ROOT / "hk" / "synthetic" / "success.json"),
    ],
)
@pytest.mark.asyncio
async def test_source_checksum_excludes_retrieved_at(
    tmp_path: Path,
    context: FetchContext,
    provider_cls: type[RegionalFixtureProvider],
    source_fixture: Path,
) -> None:
    await assert_source_checksum_excludes_retrieved_at_contract(
        provider_cls,
        source_fixture,
        tmp_path,
        context,
    )


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
    assert_registry_role_contract(provider, role_bindings, role)
