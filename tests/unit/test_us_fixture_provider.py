from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest

from macro_platform.contracts.common import Region
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
from macro_platform.contracts.news import NewsQuery
from macro_platform.contracts.provider import Dataset, FetchContext
from macro_platform.providers.base import (
    MacroDataProvider,
    MarketDataProvider,
    NewsProvider,
    ProviderAuthenticationError,
    ProviderAuthorizationError,
    ProviderCursorError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderTimeoutError,
)
from macro_platform.providers.registry import ProviderRegistry
from macro_platform.providers.us import (
    US_PROVIDER_ID,
    US_ROLE_BINDINGS,
    UsFixtureProvider,
    register_us_provider_roles,
)

NOW = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)
CONTEXT = FetchContext(
    request_id=UUID("00000000-0000-4000-8000-000000000006"),
    as_of=NOW,
    deadline_at=datetime(2026, 7, 23, 8, 1, tzinfo=UTC),
)


@pytest.fixture
def provider() -> UsFixtureProvider:
    return UsFixtureProvider.from_fixture("success", clock=lambda: NOW)


@pytest.mark.asyncio
async def test_us_fixture_provider_maps_all_vertical_slice_datasets(
    provider: UsFixtureProvider,
) -> None:
    instruments = await provider.fetch_instruments(InstrumentQuery(regions={Region.US}), CONTEXT)
    assert len(instruments.items) == 1
    instrument = instruments.items[0]
    assert instrument.canonical_symbol == "XNAS:AAPL"
    assert instrument.source.source_symbol == "aapl"
    assert instrument.source.checksum_sha256
    repeated_instruments = await provider.fetch_instruments(
        InstrumentQuery(regions={Region.US}), CONTEXT
    )
    assert repeated_instruments.items == instruments.items

    bars = await provider.fetch_bars(
        BarQuery(
            instrument_ids=[instrument.instrument_id],
            interval=Interval.D1,
            start=datetime(2026, 7, 22, tzinfo=UTC),
            end=datetime(2026, 7, 23, tzinfo=UTC),
            adjustment=Adjustment.RAW,
            as_of=NOW,
        ),
        CONTEXT,
    )
    assert len(bars.items) == 1
    assert bars.items[0].trading_date == date(2026, 7, 22)
    assert bars.items[0].bar_start == datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
    assert bars.items[0].available_at <= NOW

    observations = await provider.fetch_market_observations(
        MarketObservationQuery(
            regions={Region.US},
            metric_codes=["rate.fed_funds.effective"],
            start=datetime(2026, 7, 21, tzinfo=UTC),
            end=datetime(2026, 7, 24, tzinfo=UTC),
            as_of=NOW,
        ),
        CONTEXT,
    )
    assert observations.items[0].unit == "percent"
    assert observations.items[0].value is not None

    series = await provider.fetch_macro_series(MacroSeriesQuery(regions={Region.US}), CONTEXT)
    assert [item.series_id for item in series.items] == ["macro:US:BLS:CPI_ALL_ITEMS"]

    macro_observations = await provider.fetch_macro_observations(
        MacroObservationQuery(
            series_ids=["macro:US:BLS:CPI_ALL_ITEMS"],
            period_from=date(2026, 6, 1),
            period_to=date(2026, 6, 30),
            as_of=NOW,
        ),
        CONTEXT,
    )
    assert macro_observations.items[0].available_at <= NOW

    releases = await provider.fetch_macro_releases(
        MacroReleaseQuery(
            regions={Region.US},
            scheduled_from=datetime(2026, 7, 1, tzinfo=UTC),
            scheduled_to=datetime(2026, 8, 1, tzinfo=UTC),
            as_of=NOW,
        ),
        CONTEXT,
    )
    assert releases.items[0].release_name == "Consumer Price Index"

    news = await provider.fetch_news(
        NewsQuery(
            regions={Region.US},
            published_from=datetime(2026, 7, 1, tzinfo=UTC),
            published_to=datetime(2026, 8, 1, tzinfo=UTC),
            as_of=NOW,
        ),
        CONTEXT,
    )
    assert news.items[0].news_id.startswith("news_us_sec_")
    assert news.items[0].body is None
    assert news.items[0].usage_rights.external_llm_allowed is False


def test_us_fixture_provider_declares_stable_roles_and_capabilities(
    provider: UsFixtureProvider,
) -> None:
    capabilities = provider.capabilities()
    assert capabilities.provider_id == US_PROVIDER_ID
    assert capabilities.regions == {Region.US}
    assert capabilities.datasets == set(Dataset)
    assert capabilities.supports_full_text is False
    assert capabilities.external_llm_allowed is False

    registry = ProviderRegistry()
    register_us_provider_roles(registry, provider)
    for role in US_ROLE_BINDINGS:
        assert registry.resolve(role) is provider


def test_us_fixture_provider_implements_all_required_provider_protocols(
    provider: UsFixtureProvider,
) -> None:
    market_provider: MarketDataProvider = provider
    macro_provider: MacroDataProvider = provider
    news_provider: NewsProvider = provider

    assert market_provider is provider
    assert macro_provider is provider
    assert news_provider is provider


@pytest.mark.parametrize(
    ("fixture_name", "error_type"),
    [
        ("auth_failure", ProviderAuthenticationError),
        ("forbidden", ProviderAuthorizationError),
        ("rate_limited", ProviderRateLimitError),
        ("timeout", ProviderTimeoutError),
        ("missing_fields", ProviderSchemaError),
        ("schema_changed", ProviderSchemaError),
        ("malformed_json", ProviderSchemaError),
        ("html_login", ProviderAuthorizationError),
        ("duplicate_page", ProviderCursorError),
    ],
)
@pytest.mark.asyncio
async def test_us_fixture_provider_never_turns_provider_failures_into_empty_data(
    fixture_name: str,
    error_type: type[Exception],
) -> None:
    provider = UsFixtureProvider.from_fixture(fixture_name, clock=lambda: NOW)

    with pytest.raises(error_type):
        await provider.fetch_instruments(InstrumentQuery(regions={Region.US}), CONTEXT)


@pytest.mark.asyncio
async def test_us_fixture_provider_returns_an_explicit_empty_page() -> None:
    provider = UsFixtureProvider.from_fixture("empty", clock=lambda: NOW)

    page = await provider.fetch_instruments(InstrumentQuery(regions={Region.US}), CONTEXT)

    assert page.items == []
    assert page.complete is True
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_us_fixture_provider_enforces_pit_and_rejects_unknown_cursors(
    provider: UsFixtureProvider,
) -> None:
    before_observation = datetime(2026, 7, 22, 21, 0, tzinfo=UTC)
    pit_context = FetchContext(
        request_id=CONTEXT.request_id,
        as_of=before_observation,
        deadline_at=CONTEXT.deadline_at,
    )
    pit_page = await provider.fetch_market_observations(
        MarketObservationQuery(
            regions={Region.US},
            metric_codes=["rate.fed_funds.effective"],
            start=datetime(2026, 7, 21, tzinfo=UTC),
            end=datetime(2026, 7, 24, tzinfo=UTC),
            as_of=before_observation,
        ),
        pit_context,
    )
    assert pit_page.items == []

    with pytest.raises(ProviderCursorError):
        await provider.fetch_instruments(
            InstrumentQuery(regions={Region.US}, cursor="not-a-provider-cursor"), CONTEXT
        )


@pytest.mark.asyncio
async def test_us_fixture_source_checksum_excludes_retrieved_at(tmp_path: Path) -> None:
    source_fixture = UsFixtureProvider.fixture_dir / "success.json"
    changed_fixture = tmp_path / "changed-retrieved-at.json"
    payload = json.loads(source_fixture.read_text(encoding="utf-8"))
    payload["pages"]["instruments"]["items"][0]["retrieved_at"] = "2026-07-23T07:30:00Z"
    changed_fixture.write_text(json.dumps(payload), encoding="utf-8")

    original = UsFixtureProvider.from_fixture("success", clock=lambda: NOW)
    changed = UsFixtureProvider(changed_fixture, clock=lambda: NOW)
    query = InstrumentQuery(regions={Region.US})

    original_page = await original.fetch_instruments(query, CONTEXT)
    changed_page = await changed.fetch_instruments(query, CONTEXT)

    assert (
        original_page.items[0].source.checksum_sha256
        == changed_page.items[0].source.checksum_sha256
    )
