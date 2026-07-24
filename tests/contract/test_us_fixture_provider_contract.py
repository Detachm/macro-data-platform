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
from macro_platform.contracts.news import ContentMode, NewsQuery
from macro_platform.contracts.provider import FetchContext
from macro_platform.providers.base import (
    ProviderAuthenticationError,
    ProviderAuthorizationError,
    ProviderCursorError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderTimeoutError,
)
from macro_platform.providers.us import UsFixtureProvider
from macro_platform.services.news_service import NewsService
from macro_platform.storage.repositories import EmptyDataRepository
from tests.contract.provider_suite import (
    assert_capabilities_contract,
    assert_news_contract,
    assert_page_contract,
    assert_pit_contract,
    assert_provenance_contract,
    assert_stable_page,
)

NOW = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)
CONTEXT = FetchContext(
    request_id=UUID("00000000-0000-4000-8000-000000000007"),
    as_of=NOW,
    deadline_at=datetime(2026, 7, 23, 8, 1, tzinfo=UTC),
)
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "us" / "provider"


class FixtureNewsRepository(EmptyDataRepository):
    def __init__(self, events: list) -> None:
        self._events = events

    async def list_news(self, query: NewsQuery) -> list:
        return self._events


@pytest.fixture
def provider() -> UsFixtureProvider:
    return UsFixtureProvider.from_fixture("success", clock=lambda: NOW)


def _success_payload() -> dict:
    return json.loads((FIXTURE_DIR / "success.json").read_text(encoding="utf-8"))


def _write_fixture(tmp_path: Path, name: str, payload: dict) -> UsFixtureProvider:
    fixture_path = tmp_path / name
    fixture_path.write_text(json.dumps(payload), encoding="utf-8")
    return UsFixtureProvider(fixture_path, clock=lambda: NOW)


async def test_us_fixture_provider_passes_shared_contract_suite(
    provider: UsFixtureProvider,
) -> None:
    assert_capabilities_contract(provider)

    instruments = await provider.fetch_instruments(InstrumentQuery(regions={Region.US}), CONTEXT)
    aapl_id = next(
        item.instrument_id for item in instruments.items if item.canonical_symbol == "XNAS:AAPL"
    )
    index_id = next(
        item.instrument_id for item in instruments.items if item.canonical_symbol == "BATS:SPX"
    )
    bars = await provider.fetch_bars(
        BarQuery(
            instrument_ids=[aapl_id, index_id],
            interval=Interval.D1,
            start=datetime(2026, 7, 22, tzinfo=UTC),
            end=datetime(2026, 7, 23, tzinfo=UTC),
            adjustment=Adjustment.RAW,
            as_of=NOW,
        ),
        CONTEXT,
    )
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
    series = await provider.fetch_macro_series(MacroSeriesQuery(regions={Region.US}), CONTEXT)
    macro_observations = await provider.fetch_macro_observations(
        MacroObservationQuery(
            series_ids=["macro:US:BLS:CPI_ALL_ITEMS"],
            period_from=date(2026, 6, 1),
            period_to=date(2026, 6, 30),
            as_of=NOW,
        ),
        CONTEXT,
    )
    releases = await provider.fetch_macro_releases(
        MacroReleaseQuery(
            regions={Region.US},
            scheduled_from=datetime(2026, 7, 1, tzinfo=UTC),
            scheduled_to=datetime(2026, 8, 1, tzinfo=UTC),
            as_of=NOW,
        ),
        CONTEXT,
    )
    news = await provider.fetch_news(
        NewsQuery(
            regions={Region.US},
            published_from=datetime(2026, 7, 1, tzinfo=UTC),
            published_to=datetime(2026, 8, 1, tzinfo=UTC),
            as_of=NOW,
        ),
        CONTEXT,
    )

    for page in [instruments, bars, observations, series, macro_observations, releases, news]:
        assert_page_contract(page)
        assert_provenance_contract(page)
        assert all(item.source.source_url is not None for item in page.items)
    for page in [bars, observations, macro_observations, releases, news]:
        assert_pit_contract(page, as_of=NOW)
    assert_news_contract(news)
    assert all(item.body is None for item in news.items)
    assert all(
        item.content_mode in {ContentMode.HEADLINE, ContentMode.SNIPPET} for item in news.items
    )

    repeated_news = await provider.fetch_news(
        NewsQuery(
            regions={Region.US},
            published_from=datetime(2026, 7, 1, tzinfo=UTC),
            published_to=datetime(2026, 8, 1, tzinfo=UTC),
            as_of=NOW,
        ),
        CONTEXT,
    )
    assert_stable_page(news, repeated_news)


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
async def test_us_fixture_provider_contract_failures_are_never_empty_pages(
    fixture_name: str,
    error_type: type[Exception],
) -> None:
    provider = UsFixtureProvider.from_fixture(fixture_name, clock=lambda: NOW)

    with pytest.raises(error_type):
        await provider.fetch_instruments(InstrumentQuery(regions={Region.US}), CONTEXT)


async def test_us_fixture_provider_contract_honors_half_open_ranges_and_stable_order(
    tmp_path: Path,
) -> None:
    payload = _success_payload()
    end_bar = {
        **payload["pages"]["bars"]["items"][0],
        "record_id": "polygon-aapl-2026-07-23",
        "bar_start": "2026-07-23T13:30:00Z",
        "bar_end": "2026-07-23T20:00:00Z",
        "trading_date": "2026-07-23",
    }
    payload["pages"]["bars"]["items"].append(end_bar)
    provider = _write_fixture(tmp_path, "range-and-order.json", payload)
    instruments = await provider.fetch_instruments(InstrumentQuery(regions={Region.US}), CONTEXT)
    instrument_ids = [item.instrument_id for item in instruments.items]

    page = await provider.fetch_bars(
        BarQuery(
            instrument_ids=instrument_ids,
            interval=Interval.D1,
            start=datetime(2026, 7, 22, tzinfo=UTC),
            end=datetime(2026, 7, 23, 13, 30, tzinfo=UTC),
            adjustment=Adjustment.RAW,
            as_of=NOW,
        ),
        CONTEXT,
    )

    assert [item.bar_start for item in page.items] == [
        datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
    ] * 2
    assert [(item.bar_end, item.instrument_id, item.bar_id) for item in page.items] == sorted(
        (item.bar_end, item.instrument_id, item.bar_id) for item in page.items
    )


async def test_us_fixture_provider_contract_parses_dst_offset_bar_fixture() -> None:
    provider = UsFixtureProvider.from_fixture("dst_offset", clock=lambda: NOW)
    aapl_id = "ins_us_5249d2b8f8155772"

    page = await provider.fetch_bars(
        BarQuery(
            instrument_ids=[aapl_id],
            interval=Interval.D1,
            start=datetime(2026, 3, 9, 13, 30, tzinfo=UTC),
            end=datetime(2026, 3, 10, tzinfo=UTC),
            adjustment=Adjustment.RAW,
            as_of=NOW,
        ),
        CONTEXT,
    )

    assert page.items[0].bar_start == datetime(2026, 3, 9, 13, 30, tzinfo=UTC)
    assert page.items[0].trading_date == date(2026, 3, 9)


async def test_us_fixture_provider_contract_covers_news_002_003_012_013_and_017(
    provider: UsFixtureProvider,
) -> None:
    payload = _success_payload()
    daily_news = next(
        item for item in payload["pages"]["news"]["items"] if "accession_number" not in item
    )
    tracking_variant = provider._parse_news(
        {**daily_news, "canonical_url": "HTTPS://www.bls.gov/news.release/cpi.nr0.htm?utm_source=x"}
    )
    canonical_variant = provider._parse_news(
        {**daily_news, "canonical_url": "https://www.bls.gov/news.release/cpi.nr0.htm"}
    )
    title_variant = provider._parse_news({**daily_news, "title": "ＣＰＩ：  June—2026！"})
    normalized_title = provider._parse_news({**daily_news, "title": "cpi, June 2026"})
    headline_only = provider._parse_news(
        {key: value for key, value in daily_news.items() if key != "summary"}
    )
    restricted = provider._parse_news(
        {
            **daily_news,
            "rights": {**daily_news["rights"], "external_llm_allowed": False},
        }
    )

    assert tracking_variant.canonical_url == canonical_variant.canonical_url
    assert title_variant.content_hash_sha256 == normalized_title.content_hash_sha256
    assert headline_only.content_mode is ContentMode.HEADLINE
    assert headline_only.vendor_annotations == []

    events = await NewsService(FixtureNewsRepository([restricted])).events(
        NewsQuery(
            regions={Region.US},
            published_from=datetime(2026, 7, 1, tzinfo=UTC),
            published_to=datetime(2026, 8, 1, tzinfo=UTC),
            as_of=NOW,
        ),
        for_external_llm=True,
    )
    assert events[0].summary is None
    assert events[0].content_mode is ContentMode.HEADLINE


def test_us_fixture_manifest_matches_the_offline_fixture_set() -> None:
    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
    fixture_names = {
        path.name for path in FIXTURE_DIR.glob("*.json") if path.name != "manifest.json"
    }

    assert manifest["offline_only"] is True
    assert manifest["credentials"] == "none"
    assert set(manifest["fixtures"]) == fixture_names
    assert all(test_ids for test_ids in manifest["fixtures"].values())
    assert manifest["not_applicable"]["PRV-011"]["reason"]
    assert manifest["not_applicable"]["PRV-011"]["follow_up"]
