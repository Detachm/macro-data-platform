from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from macro_platform.contracts.common import SourceRef, StrictModel
from macro_platform.contracts.macro import (
    MacroObservationQuery,
    MacroReleaseQuery,
    MacroSeriesQuery,
)
from macro_platform.contracts.market import (
    Adjustment,
    BarQuery,
    InstrumentQuery,
    Interval,
    MarketObservationQuery,
)
from macro_platform.contracts.news import ContentMode, NewsQuery
from macro_platform.contracts.provider import (
    FetchContext,
    ProviderCapabilities,
    ProviderPage,
)
from macro_platform.normalization.common import (
    canonical_json_checksum,
    canonicalize_url,
    normalize_title_for_matching,
)
from macro_platform.providers._regional_fixture import RegionalFixtureProvider
from macro_platform.providers.base import BaseProvider

ContractStatus = Literal["implemented", "xfail"]

REQUIRED_CONTRACT_CASE_IDS = tuple(f"PRV-{index:03d}" for index in range(1, 21)) + (
    "NEWS-002",
    "NEWS-003",
    "NEWS-012",
    "NEWS-013",
    "NEWS-017",
    "PIT-AVAILABLE-AT-AS-OF",
)


@dataclass(frozen=True)
class ContractCase:
    case_id: str
    status: ContractStatus
    blocked_by: tuple[str, ...] = ()

    @property
    def xfail_reason(self) -> str:
        return f"{self.case_id} blocked by {', '.join(self.blocked_by)}"


CONTRACT_CASES = {
    case.case_id: case
    for case in (
        ContractCase("PRV-001", "implemented"),
        ContractCase("PRV-002", "implemented"),
        ContractCase("PRV-003", "xfail", ("#5",)),
        ContractCase("PRV-004", "xfail", ("#5",)),
        ContractCase("PRV-005", "xfail", ("#5",)),
        ContractCase("PRV-006", "xfail", ("#5", "#3")),
        ContractCase("PRV-007", "implemented"),
        ContractCase("PRV-008", "implemented"),
        ContractCase("PRV-009", "implemented"),
        ContractCase("PRV-010", "implemented"),
        ContractCase("PRV-011", "xfail", ("#20",), "requires persisted unsupported-PIT evidence"),
        ContractCase("PRV-012", "xfail", ("#20",), "requires raw-timezone audit persistence"),
        ContractCase("PRV-013", "implemented"),
        ContractCase("PRV-014", "xfail", ("#20",), "requires transactional retry persistence"),
        ContractCase("PRV-015", "xfail", ("#21",), "requires fixture cursor continuation protocol"),
        ContractCase(
            "PRV-016", "xfail", ("#20",), "requires committed provider watermark recovery"
        ),
        ContractCase("PRV-017", "implemented"),
        ContractCase("PRV-018", "implemented"),
        ContractCase("PRV-019", "implemented"),
        ContractCase("PRV-020", "implemented"),
        ContractCase("NEWS-002", "implemented"),
        ContractCase("NEWS-003", "implemented"),
        ContractCase("NEWS-012", "implemented"),
        ContractCase("NEWS-013", "implemented"),
        ContractCase("NEWS-017", "implemented"),
        ContractCase("PIT-AVAILABLE-AT-AS-OF", "implemented"),
    )
}


def assert_capabilities_contract(provider: BaseProvider) -> ProviderCapabilities:
    capabilities = provider.capabilities()
    assert capabilities.provider_id.strip() == capabilities.provider_id
    assert capabilities.regions
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


def assert_source_ref_contract(source: SourceRef, provider_id: str) -> None:
    assert source.provider_id == provider_id
    assert source.provider_record_id
    assert source.source_name
    assert source.retrieved_at.tzinfo is not None
    assert len(source.checksum_sha256) == 64
    assert source.checksum_sha256.lower() == source.checksum_sha256


def assert_page_provenance(page: ProviderPage[StrictModel], provider_id: str) -> None:
    for item in page.items:
        assert_source_ref_contract(item.source, provider_id)  # type: ignore[attr-defined]


def assert_available_at_not_after_as_of(page: ProviderPage[StrictModel], as_of: datetime) -> None:
    for item in page.items:
        available_at = getattr(item, "available_at", None)
        if available_at is not None:
            assert available_at <= as_of


def assert_fixture_manifest_contract(manifest_path: Path) -> dict[str, object]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["fixture_kind"] == "synthetic"

    fixtures = manifest["fixtures"]
    assert isinstance(fixtures, list)
    for fixture_name in fixtures:
        assert (manifest_path.parent / "synthetic" / f"{fixture_name}.json").exists()

    case_entries = manifest["contract_cases"]
    assert isinstance(case_entries, list)
    case_ids = {entry["id"] for entry in case_entries}
    assert set(REQUIRED_CONTRACT_CASE_IDS).issubset(case_ids)
    for entry in case_entries:
        case = CONTRACT_CASES[entry["id"]]
        assert entry["status"] == case.status
        if case.status == "xfail":
            assert entry["blocked_by"] == list(case.blocked_by)
    return manifest


async def assert_success_fixture_contract(
    provider: RegionalFixtureProvider, context: FetchContext
) -> None:
    region = next(iter(provider.region_set()))
    capabilities = assert_capabilities_contract(provider)
    assert capabilities.regions == {region}
    assert capabilities.datasets == provider.live_ready_datasets

    instrument_query = InstrumentQuery(regions={region}, limit=100)
    instrument_query_before = instrument_query.model_dump(mode="json")
    instruments = await provider.fetch_instruments(instrument_query, context)
    assert instrument_query.model_dump(mode="json") == instrument_query_before
    assert_page_contract(instruments)
    assert_page_provenance(instruments, provider.provider_id)
    assert_stable_page(instruments, await provider.fetch_instruments(instrument_query, context))
    assert len(instruments.items) == 1

    bar_query = BarQuery(
        instrument_ids=[instruments.items[0].instrument_id],
        interval=Interval.D1,
        start=datetime(2026, 7, 21, tzinfo=UTC),
        end=datetime(2026, 7, 23, tzinfo=UTC),
        adjustment=Adjustment.RAW,
        as_of=context.as_of,
    )
    bar_query_before = bar_query.model_dump(mode="json")
    bars = await provider.fetch_bars(bar_query, context)
    assert bar_query.model_dump(mode="json") == bar_query_before
    assert_page_contract(bars)
    assert_page_provenance(bars, provider.provider_id)
    assert_available_at_not_after_as_of(bars, bar_query.as_of)
    assert_stable_page(bars, await provider.fetch_bars(bar_query, context))
    assert bars.items[0].quality_flags == ["synthetic"]

    market_query = MarketObservationQuery(
        regions={region},
        metric_codes=["market.turnover"],
        start=datetime(2026, 7, 22, tzinfo=UTC),
        end=datetime(2026, 7, 23, tzinfo=UTC),
        as_of=context.as_of,
    )
    market_query_before = market_query.model_dump(mode="json")
    market_observations = await provider.fetch_market_observations(market_query, context)
    assert market_query.model_dump(mode="json") == market_query_before
    assert_page_contract(market_observations)
    assert_page_provenance(market_observations, provider.provider_id)
    assert_available_at_not_after_as_of(market_observations, market_query.as_of)
    assert len(market_observations.items) == 1

    macro_series_query = MacroSeriesQuery(regions={region}, series_ids=[], limit=100)
    macro_series_query_before = macro_series_query.model_dump(mode="json")
    macro_series = await provider.fetch_macro_series(macro_series_query, context)
    assert macro_series_query.model_dump(mode="json") == macro_series_query_before
    assert_page_contract(macro_series)
    assert_page_provenance(macro_series, provider.provider_id)
    assert len(macro_series.items) == 1
    series_id = macro_series.items[0].series_id

    macro_observation_query = MacroObservationQuery(
        series_ids=[series_id],
        period_from=date(2026, 6, 1),
        period_to=date(2026, 6, 30),
        as_of=context.as_of,
    )
    macro_observation_query_before = macro_observation_query.model_dump(mode="json")
    macro_observations = await provider.fetch_macro_observations(
        macro_observation_query,
        context,
    )
    assert macro_observation_query.model_dump(mode="json") == macro_observation_query_before
    assert_page_contract(macro_observations)
    assert_page_provenance(macro_observations, provider.provider_id)
    assert_available_at_not_after_as_of(macro_observations, macro_observation_query.as_of)
    assert len(macro_observations.items) == 1

    macro_release_query = MacroReleaseQuery(
        regions={region},
        scheduled_from=datetime(2026, 7, 1, tzinfo=UTC),
        scheduled_to=datetime(2026, 7, 24, tzinfo=UTC),
        as_of=context.as_of,
    )
    macro_release_query_before = macro_release_query.model_dump(mode="json")
    macro_releases = await provider.fetch_macro_releases(macro_release_query, context)
    assert macro_release_query.model_dump(mode="json") == macro_release_query_before
    assert_page_contract(macro_releases)
    assert_page_provenance(macro_releases, provider.provider_id)
    assert_available_at_not_after_as_of(macro_releases, macro_release_query.as_of)
    assert len(macro_releases.items) == 1

    news_query = NewsQuery(
        regions={region},
        published_from=datetime(2026, 7, 22, tzinfo=UTC),
        published_to=datetime(2026, 7, 24, tzinfo=UTC),
        as_of=context.as_of,
        content_mode=ContentMode.SNIPPET,
    )
    news_query_before = news_query.model_dump(mode="json")
    news = await provider.fetch_news(news_query, context)
    assert news_query.model_dump(mode="json") == news_query_before
    assert_page_contract(news)
    assert_page_provenance(news, provider.provider_id)
    assert_available_at_not_after_as_of(news, news_query.as_of)
    assert_stable_page(news, await provider.fetch_news(news_query, context))
    assert len(news.items) == 1
    assert news.items[0].body is None
    assert news.items[0].usage_rights.external_llm_allowed is False
    assert news.items[0].vendor_annotations == []
    assert news.items[0].quality_flags[0] == "synthetic"


async def assert_empty_fixture_is_explicit(
    provider: RegionalFixtureProvider, context: FetchContext
) -> None:
    page = await provider.fetch_instruments(InstrumentQuery(regions=provider.region_set()), context)
    assert_page_contract(page)
    assert page.items == []
    assert page.complete is True


async def assert_error_fixture_raises(
    provider: RegionalFixtureProvider,
    context: FetchContext,
    error_type: type[Exception],
) -> Exception:
    try:
        await provider.fetch_instruments(InstrumentQuery(regions=provider.region_set()), context)
    except error_type as exc:
        return exc
    raise AssertionError(f"expected {error_type.__name__}")


def assert_news_normalization_contract() -> None:
    assert canonicalize_url("HTTPS://EXAMPLE.TEST/a?utm_source=x&b=2&a=1") == (
        "https://example.test/a?a=1&b=2"
    )
    assert normalize_title_for_matching(" ＡＢＣ， Rate\nCUT！ ") == "abc rate cut"
    assert normalize_title_for_matching("abc rate cut") == "abc rate cut"
    assert normalize_title_for_matching("Rate,Cut") == normalize_title_for_matching("Rate Cut")
    assert normalize_title_for_matching("中國，增長") == normalize_title_for_matching("中国增长")
    assert normalize_title_for_matching("增长1-0%") != normalize_title_for_matching("增长10%")
    assert normalize_title_for_matching("不构成违约") != normalize_title_for_matching("构成违约")


def assert_canonical_checksum_contract() -> None:
    first = canonical_json_checksum({"b": 2, "a": 1})
    second = canonical_json_checksum({"a": 1, "b": 2})
    assert first == second
    assert len(first) == 64
    assert first == first.lower()


async def assert_title_only_news_contract(
    provider: RegionalFixtureProvider, context: FetchContext
) -> None:
    region = next(iter(provider.region_set()))
    query = NewsQuery(
        regions={region},
        published_from=datetime(2026, 7, 22, tzinfo=UTC),
        published_to=datetime(2026, 7, 24, tzinfo=UTC),
        as_of=context.as_of,
        content_mode=ContentMode.HEADLINE,
    )
    page = await provider.fetch_news(query, context)
    assert_page_contract(page)
    assert_page_provenance(page, provider.provider_id)
    assert_available_at_not_after_as_of(page, query.as_of)
    assert len(page.items) == 1
    item = page.items[0]
    assert item.content_mode is ContentMode.HEADLINE
    assert item.summary is None
    assert item.body is None
    assert item.vendor_annotations == []
    assert item.usage_rights.storage_allowed is True
    assert item.quality_flags[0] == "synthetic"


async def assert_news_identity_contract(
    provider_cls: type[RegionalFixtureProvider],
    source_fixture: Path,
    temporary_directory: Path,
    context: FetchContext,
) -> None:
    """Exercise NEWS-002/003 through each regional provider entry point."""

    original_fixture, changed_fixture, original_news = _prepare_news_identity_fixtures(
        source_fixture,
        temporary_directory,
    )

    original_provider = provider_cls(original_fixture)
    region = next(iter(original_provider.region_set()))
    query = NewsQuery(
        regions={region},
        published_from=datetime(2026, 7, 22, tzinfo=UTC),
        published_to=datetime(2026, 7, 24, tzinfo=UTC),
        as_of=context.as_of,
        content_mode=ContentMode.SNIPPET,
    )
    original = await original_provider.fetch_news(query, context)
    changed = await provider_cls(changed_fixture).fetch_news(query, context)

    assert str(original.items[0].canonical_url) == "https://example.test/news/001?a=1&b=2"
    assert original.items[0].news_id == changed.items[0].news_id
    assert original.items[0].cluster_id == changed.items[0].cluster_id
    assert original.items[0].content_hash_sha256 != changed.items[0].content_hash_sha256
    assert original.items[0].content_hash_sha256 == canonical_json_checksum(
        {
            "title": original_news["title"],
            "summary": original_news["summary"],
            "body": original_news["body"],
        }
    )


async def assert_title_fallback_news_identity_contract(
    provider_cls: type[RegionalFixtureProvider],
    source_fixture: Path,
    temporary_directory: Path,
    context: FetchContext,
) -> None:
    """Verify stable NEWS-003 identity when the canonical URL is absent."""

    first_fixture, second_fixture = _prepare_title_fallback_news_identity_fixtures(
        source_fixture,
        temporary_directory,
    )

    first_provider = provider_cls(first_fixture)
    region = next(iter(first_provider.region_set()))
    query = NewsQuery(
        regions={region},
        published_from=datetime(2026, 7, 22, tzinfo=UTC),
        published_to=datetime(2026, 7, 24, tzinfo=UTC),
        as_of=context.as_of,
        content_mode=ContentMode.SNIPPET,
    )
    first = await first_provider.fetch_news(query, context)
    second = await provider_cls(second_fixture).fetch_news(query, context)

    assert first.items[0].canonical_url is None
    assert first.items[0].news_id == second.items[0].news_id
    assert first.items[0].cluster_id == second.items[0].cluster_id
    assert first.items[0].content_hash_sha256 != second.items[0].content_hash_sha256


def _prepare_news_identity_fixtures(
    source_fixture: Path,
    temporary_directory: Path,
) -> tuple[Path, Path, dict[str, object]]:
    original_fixture = temporary_directory / "success_original_news_identity_inputs.json"
    changed_fixture = temporary_directory / "success_changed_news_identity_inputs.json"
    payload = json.loads(source_fixture.read_text(encoding="utf-8"))
    changed_payload = json.loads(source_fixture.read_text(encoding="utf-8"))
    original_news = payload["pages"]["news"]["items"][0]
    changed_news = changed_payload["pages"]["news"]["items"][0]
    assert isinstance(original_news, dict)
    assert isinstance(changed_news, dict)
    original_news["canonical_url"] = "HTTPS://EXAMPLE.TEST/news/001?utm_source=x&b=2&a=1"
    changed_news["canonical_url"] = "https://example.test/news/001?b=2&utm_medium=y&a=1"
    original_news["title"] = " ＡＢＣ， Rate\nCUT！ "
    changed_news["title"] = "abc rate cut"
    original_news["entities"] = [
        {"entity_type": "organization", "entity_id": "org-central-bank", "confidence": "1"},
        {"entity_type": "country", "entity_id": "country-region", "confidence": "1"},
    ]
    changed_news["entities"] = list(reversed(original_news["entities"]))
    original_fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    changed_fixture.write_text(json.dumps(changed_payload, ensure_ascii=False), encoding="utf-8")
    return original_fixture, changed_fixture, original_news


def _prepare_title_fallback_news_identity_fixtures(
    source_fixture: Path,
    temporary_directory: Path,
) -> tuple[Path, Path]:
    first_fixture = temporary_directory / "news_title_fallback_first.json"
    second_fixture = temporary_directory / "news_title_fallback_second.json"
    first_payload = json.loads(source_fixture.read_text(encoding="utf-8"))
    second_payload = json.loads(source_fixture.read_text(encoding="utf-8"))
    first_news = first_payload["pages"]["news"]["items"][0]
    second_news = second_payload["pages"]["news"]["items"][0]
    assert isinstance(first_news, dict)
    assert isinstance(second_news, dict)
    first_news["canonical_url"] = None
    second_news["canonical_url"] = None
    first_news["title"] = "中國，開發銀行：Rate,Cut"
    second_news["title"] = "中国开发银行 Rate Cut"
    first_fixture.write_text(json.dumps(first_payload, ensure_ascii=False), encoding="utf-8")
    second_fixture.write_text(json.dumps(second_payload, ensure_ascii=False), encoding="utf-8")
    return first_fixture, second_fixture


def _canonical_items(items: Sequence[StrictModel]) -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in items]
