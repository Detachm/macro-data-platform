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


def _news_query() -> NewsQuery:
    return NewsQuery(
        regions={Region.US},
        published_from=datetime(2026, 7, 1, tzinfo=UTC),
        published_to=datetime(2026, 8, 1, tzinfo=UTC),
        as_of=NOW,
    )


def _daily_news(payload: dict) -> dict:
    return next(
        item for item in payload["pages"]["news"]["items"] if "accession_number" not in item
    )


@pytest.mark.parametrize(
    "_test_id",
    [
        pytest.param("PRV-001", id="PRV-001"),
        pytest.param("PRV-002", id="PRV-002"),
        pytest.param("PRV-005", id="PRV-005"),
        pytest.param("PRV-013", id="PRV-013"),
    ],
)
async def test_us_fixture_provider_passes_shared_contract_suite(
    provider: UsFixtureProvider,
    _test_id: str,
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
        pytest.param("auth_failure", ProviderAuthenticationError, id="PRV-008-auth"),
        pytest.param("forbidden", ProviderAuthorizationError, id="PRV-008-forbidden"),
        pytest.param("rate_limited", ProviderRateLimitError, id="PRV-007"),
        pytest.param("timeout", ProviderTimeoutError, id="PRV-010"),
        pytest.param("missing_fields", ProviderSchemaError, id="PRV-009-missing-fields"),
        pytest.param("schema_changed", ProviderSchemaError, id="PRV-020"),
        pytest.param("malformed_json", ProviderSchemaError, id="PRV-021"),
        pytest.param("html_login", ProviderAuthorizationError, id="PRV-019"),
        pytest.param("duplicate_page", ProviderCursorError, id="invalid-same-page-duplicate"),
    ],
)
async def test_us_fixture_provider_contract_failures_are_never_empty_pages(
    fixture_name: str,
    error_type: type[Exception],
) -> None:
    provider = UsFixtureProvider.from_fixture(fixture_name, clock=lambda: NOW)

    with pytest.raises(error_type):
        await provider.fetch_instruments(InstrumentQuery(regions={Region.US}), CONTEXT)


@pytest.mark.parametrize(
    "_test_id",
    [pytest.param("PRV-004", id="PRV-004"), pytest.param("PRV-005", id="PRV-005")],
)
async def test_us_fixture_provider_contract_honors_half_open_ranges_and_stable_order(
    tmp_path: Path,
    _test_id: str,
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


@pytest.mark.parametrize(
    "_test_id",
    [pytest.param("TIME-005", id="TIME-005")],
)
async def test_us_fixture_provider_contract_parses_dst_offset_bar_fixture(
    _test_id: str,
) -> None:
    provider = UsFixtureProvider.from_fixture("dst_offset", clock=lambda: NOW)
    aapl_id = "ins_us_5249d2b8f8155772"
    raw_bar = json.loads((FIXTURE_DIR / "dst_offset.json").read_text(encoding="utf-8"))["pages"][
        "bars"
    ]["items"][0]

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
    assert raw_bar["bar_start"] == "2026-03-09T09:30:00-04:00"
    assert raw_bar["bar_end"] == "2026-03-09T16:00:00-04:00"


@pytest.mark.parametrize(
    "test_id",
    [
        pytest.param("NEWS-002", id="NEWS-002"),
        pytest.param("NEWS-003", id="NEWS-003"),
        pytest.param("NEWS-012", id="NEWS-012"),
        pytest.param("NEWS-013", id="NEWS-013"),
        pytest.param("NEWS-017", id="NEWS-017"),
    ],
)
async def test_us_fixture_provider_contract_covers_news_002_003_012_013_and_017(
    tmp_path: Path,
    test_id: str,
) -> None:
    payload = _success_payload()
    daily_news = _daily_news(payload)
    if test_id == "NEWS-002":
        daily_news.update(
            record_id="news-002",
            canonical_url="HTTPS://www.bls.gov/news.release/cpi.nr0.htm?utm_source=x",
        )
    elif test_id == "NEWS-003":
        daily_news.update(record_id="news-003-full-width", title="ＣＰＩ：  June—2026！")
        payload["pages"]["news"]["items"].append(
            {
                **daily_news,
                "record_id": "news-003-normalized",
                "title": "cpi, June 2026",
            }
        )
    elif test_id == "NEWS-012":
        daily_news.update(record_id="news-012")
        daily_news.pop("summary")
    elif test_id == "NEWS-013":
        daily_news.update(record_id="news-013")
    else:
        daily_news.update(
            record_id="news-017",
            rights={**daily_news["rights"], "external_llm_allowed": False},
        )

    provider = _write_fixture(tmp_path, f"{test_id}.json", payload)
    query = _news_query()
    if test_id == "NEWS-012":
        query = query.model_copy(update={"content_mode": ContentMode.HEADLINE})
    page = await provider.fetch_news(query, CONTEXT)

    if test_id == "NEWS-002":
        event = next(
            item for item in page.items if item.source.provider_record_id.endswith(":news-002")
        )
        assert str(event.canonical_url) == "https://www.bls.gov/news.release/cpi.nr0.htm"
    elif test_id == "NEWS-003":
        variants = [
            item
            for item in page.items
            if item.source.provider_record_id.endswith(
                (":news-003-full-width", ":news-003-normalized")
            )
        ]
        assert len(variants) == 2
        assert len({item.content_hash_sha256 for item in variants}) == 1
    elif test_id == "NEWS-012":
        event = next(
            item for item in page.items if item.source.provider_record_id.endswith(":news-012")
        )
        assert event.content_mode is ContentMode.HEADLINE
    elif test_id == "NEWS-013":
        event = next(
            item for item in page.items if item.source.provider_record_id.endswith(":news-013")
        )
        assert event.vendor_annotations == []
    else:
        restricted = next(
            item for item in page.items if item.source.provider_record_id.endswith(":news-017")
        )
        events = await NewsService(FixtureNewsRepository([restricted])).events(_news_query())
        assert events[0].summary is not None
        assert events[0].content_mode is ContentMode.SNIPPET


@pytest.mark.parametrize("_test_id", [pytest.param("PRV-017", id="PRV-017")])
async def test_us_fixture_provider_contract_uses_canonical_source_checksum(
    tmp_path: Path,
    _test_id: str,
) -> None:
    payload = _success_payload()
    original = _write_fixture(tmp_path, "checksum-original.json", payload)
    reordered_path = tmp_path / "checksum-reordered.json"
    reordered_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    reordered = UsFixtureProvider(reordered_path, clock=lambda: NOW)

    original_page = await original.fetch_instruments(InstrumentQuery(regions={Region.US}), CONTEXT)
    reordered_page = await reordered.fetch_instruments(
        InstrumentQuery(regions={Region.US}), CONTEXT
    )
    original_aapl = next(
        item for item in original_page.items if item.canonical_symbol == "XNAS:AAPL"
    )
    reordered_aapl = next(
        item for item in reordered_page.items if item.canonical_symbol == "XNAS:AAPL"
    )

    assert original_aapl.source.checksum_sha256 == reordered_aapl.source.checksum_sha256


@pytest.mark.parametrize("_test_id", [pytest.param("PIT-009", id="PIT-009")])
async def test_us_fixture_provider_contract_filters_future_records_from_every_pit_dataset(
    tmp_path: Path,
    _test_id: str,
) -> None:
    payload = _success_payload()
    future = "2026-07-23T08:01:00Z"
    future_bar = {
        **payload["pages"]["bars"]["items"][0],
        "record_id": "future-bar",
        "provider_updated_at": future,
        "bar_start": "2026-07-23T13:30:00Z",
        "bar_end": "2026-07-23T20:00:00Z",
        "trading_date": "2026-07-23",
        "available_at": future,
    }
    future_observation = {
        **payload["pages"]["market_observations"]["items"][0],
        "record_id": "future-observation",
        "provider_updated_at": future,
        "period_start": "2026-07-23T00:00:00Z",
        "period_end": "2026-07-24T00:00:00Z",
        "observed_at": "2026-07-23T08:00:00Z",
        "available_at": future,
    }
    future_macro_observation = {
        **payload["pages"]["macro_observations"]["items"][0],
        "record_id": "future-macro-observation",
        "provider_updated_at": future,
        "released_at": future,
        "available_at": future,
        "vintage_id": future,
        "revision_no": 1,
    }
    future_release = {
        **payload["pages"]["macro_releases"]["items"][0],
        "record_id": "future-macro-release",
        "provider_updated_at": future,
        "scheduled_at": "2026-07-23T08:00:00Z",
        "released_at": future,
        "available_at": future,
    }
    future_news = {
        **_daily_news(payload),
        "record_id": "future-news",
        "provider_updated_at": future,
        "published_at": "2026-07-22T12:30:00Z",
        "first_seen_at": future,
        "available_at": future,
    }
    payload["pages"]["bars"]["items"].append(future_bar)
    payload["pages"]["market_observations"]["items"].append(future_observation)
    payload["pages"]["macro_observations"]["items"].append(future_macro_observation)
    payload["pages"]["macro_releases"]["items"].append(future_release)
    payload["pages"]["news"]["items"].append(future_news)
    provider = _write_fixture(tmp_path, "future-pit.json", payload)

    instruments = await provider.fetch_instruments(InstrumentQuery(regions={Region.US}), CONTEXT)
    bars = await provider.fetch_bars(
        BarQuery(
            instrument_ids=[item.instrument_id for item in instruments.items],
            interval=Interval.D1,
            start=datetime(2026, 7, 21, tzinfo=UTC),
            end=datetime(2026, 7, 24, tzinfo=UTC),
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
    news = await provider.fetch_news(_news_query(), CONTEXT)

    for page in [bars, observations, macro_observations, releases, news]:
        assert_pit_contract(page, as_of=NOW)
        assert page.warnings == []
    assert len(bars.items) == 2
    assert len(observations.items) == 1
    assert len(macro_observations.items) == 1
    assert len(releases.items) == 1
    assert len(news.items) == 4


@pytest.mark.parametrize("_test_id", [pytest.param("PRV-003-duplicate", id="PRV-003-duplicate")])
async def test_us_fixture_provider_contract_rejects_cross_page_duplicate_records(
    tmp_path: Path,
    _test_id: str,
) -> None:
    payload = _success_payload()
    first = payload["pages"]["news"]["items"][0]
    payload["pages"] = {
        "news": [
            {"cursor": None, "next_cursor": "news-page-2", "items": [first]},
            {
                "cursor": "news-page-2",
                "next_cursor": None,
                "items": [{**first, "record_id": "replayed-with-new-raw-record-id"}],
            },
        ]
    }
    provider = _write_fixture(tmp_path, "cross-page-duplicate.json", payload)
    first_page = await provider.fetch_news(_news_query(), CONTEXT)

    with pytest.raises(ProviderCursorError, match="duplicate record id"):
        await provider.fetch_news(
            _news_query().model_copy(update={"cursor": first_page.next_cursor}), CONTEXT
        )


@pytest.mark.parametrize("_test_id", [pytest.param("PRV-003", id="PRV-003")])
async def test_us_fixture_provider_contract_returns_complete_two_page_result_without_duplicates(
    tmp_path: Path,
    _test_id: str,
) -> None:
    payload = _success_payload()
    first = _daily_news(payload)
    second = {
        **first,
        "record_id": "pagination-page-two",
        "canonical_url": "https://www.bls.gov/news.release/cpi.page-two.htm",
    }
    payload["pages"] = {
        "news": [
            {"cursor": None, "next_cursor": "news-page-2", "items": [first]},
            {"cursor": "news-page-2", "next_cursor": None, "items": [second]},
        ]
    }
    provider = _write_fixture(tmp_path, "page-replay.json", payload)
    query = _news_query()
    first_page = await provider.fetch_news(query, CONTEXT)
    second_page = await provider.fetch_news(
        query.model_copy(update={"cursor": first_page.next_cursor}), CONTEXT
    )

    assert first_page.next_cursor is not None
    assert first_page.complete is False
    assert second_page.next_cursor is None
    assert second_page.complete is True
    provider_record_ids = [
        item.source.provider_record_id for item in [*first_page.items, *second_page.items]
    ]
    assert [
        provider_record_id.rsplit(":", maxsplit=1)[-1] for provider_record_id in provider_record_ids
    ] == [
        "bls-cpi-june-2026",
        "pagination-page-two",
    ]
    assert len(provider_record_ids) == len(set(provider_record_ids))


def test_us_fixture_manifest_matches_the_offline_fixture_set() -> None:
    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
    fixture_names = {
        path.name for path in FIXTURE_DIR.glob("*.json") if path.name != "manifest.json"
    }

    assert manifest["offline_only"] is True
    assert manifest["credentials"] == "none"
    assert set(manifest["fixtures"]) == fixture_names
    assert all(test_ids for test_ids in manifest["fixtures"].values())
    for test_id in {"PRV-011", "PRV-012", "PRV-014", "PRV-016"}:
        assert manifest["not_applicable"][test_id]["reason"]
        assert (
            manifest["not_applicable"][test_id]["follow_up"]
            == "https://github.com/Detachm/macro-data-platform/issues/20"
        )
