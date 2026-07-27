from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from macro_platform.config import Settings
from macro_platform.contracts.market import Adjustment, BarQuery, Interval
from macro_platform.contracts.provider import Dataset, FetchContext
from macro_platform.providers.base import (
    ProviderAuthenticationError,
    ProviderAuthorizationError,
    ProviderCursorError,
    ProviderError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    UnsupportedCapabilityError,
)
from macro_platform.providers.registry import ProviderRegistry, ProviderRegistryError
from macro_platform.providers.us.factory import (
    create_us_provider_registry,
    create_us_provider_registry_from_settings,
)
from macro_platform.providers.us.fixture import US_FIXTURE_CONTRACT_ROLE_BINDINGS
from macro_platform.providers.us.twelve_data import (
    TWELVE_DATA_PRIMARY_ROLE,
    TwelveDataDailyBarsProvider,
    TwelveDataInstrument,
    register_us_twelve_data_provider_roles,
)

NOW = datetime(2026, 7, 23, 21, 0, tzinfo=UTC)
CONTEXT = FetchContext(
    request_id=UUID("00000000-0000-4000-8000-000000000034"),
    as_of=NOW,
    deadline_at=datetime(2026, 7, 23, 21, 1, tzinfo=UTC),
)
SPY = TwelveDataInstrument(
    instrument_id="ins_us_etf_spy",
    canonical_symbol="ARCX:SPY",
    source_symbol="SPY",
)
QQQ = TwelveDataInstrument(
    instrument_id="ins_us_etf_qqq",
    canonical_symbol="XNAS:QQQ",
    source_symbol="QQQ",
)
DIA = TwelveDataInstrument(
    instrument_id="ins_us_etf_dia",
    canonical_symbol="ARCX:DIA",
    source_symbol="DIA",
)
TEST_API_KEY = SecretStr("unit-test-api-key")


async def _no_sleep(_: float) -> None:
    return None


def _provider(
    *,
    transport: httpx.AsyncBaseTransport,
    api_key: SecretStr = TEST_API_KEY,
    no_api_key: bool = False,
    instruments: list[TwelveDataInstrument] | None = None,
    sleeper: Callable[[float], Awaitable[None]] | None = None,
) -> TwelveDataDailyBarsProvider:
    return TwelveDataDailyBarsProvider(
        api_key=None if no_api_key else api_key,
        instruments=instruments or [SPY],
        client=httpx.AsyncClient(transport=transport),
        cursor_signing_secret="unit-test-cursor-secret",
        clock=lambda: NOW,
        sleeper=sleeper or _no_sleep,
    )


def _bar_query(*, limit: int = 1000, cursor: str | None = None) -> BarQuery:
    return BarQuery(
        instrument_ids=[SPY.instrument_id],
        interval=Interval.D1,
        start=datetime(2026, 7, 22, 4, 0, tzinfo=UTC),
        end=datetime(2026, 7, 23, 4, 0, tzinfo=UTC),
        adjustment=Adjustment.RAW,
        as_of=NOW,
        limit=limit,
        cursor=cursor,
    )


def _success_payload(*, values: list[dict[str, str]] | None = None) -> dict[str, object]:
    return {
        "meta": {"symbol": "SPY", "interval": "1day", "currency": "USD"},
        "values": values
        or [
            {
                "datetime": "2026-07-22",
                "open": "610.00",
                "high": "615.00",
                "low": "609.00",
                "close": "614.00",
                "volume": "123456",
            }
        ],
    }


@pytest.mark.asyncio
async def test_PRV_001_fetch_bars_maps_raw_daily_bar_and_redacts_api_key_from_provenance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(
            "https://api.twelvedata.com/time_series?"
            "symbol=SPY&interval=1day&start_date=2026-07-22&end_date=2026-07-22&"
            "outputsize=5000&order=ASC"
        )
        assert request.headers["Authorization"] == "apikey unit-test-api-key"
        return httpx.Response(
            200,
            json=_success_payload(),
        )

    provider = _provider(transport=httpx.MockTransport(handler))
    caplog.set_level(logging.INFO, logger="httpx")
    try:
        page = await provider.fetch_bars(
            _bar_query(),
            CONTEXT,
        )
    finally:
        await provider.aclose()

    assert page.complete is True
    assert page.next_cursor is None
    assert len(page.items) == 1
    bar = page.items[0]
    assert bar.instrument_id == SPY.instrument_id
    assert bar.canonical_symbol == "ARCX:SPY"
    assert bar.trading_date.isoformat() == "2026-07-22"
    assert bar.bar_start == datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
    assert bar.bar_end == datetime(2026, 7, 22, 20, 0, tzinfo=UTC)
    assert bar.source.provider_record_id == (
        "us.twelve-data.v1:SPY:1day:2026-07-22T13:30:00+00:00:raw"
    )
    assert bar.source.source_symbol == "SPY"
    assert "apikey" not in str(bar.source.source_url)
    assert "unit-test-api-key" not in str(bar.source.source_url)
    assert "unit-test-api-key" not in caplog.text
    assert bar.available_at == NOW
    assert bar.source.retrieved_at == NOW


def test_GOV_026_twelve_data_rejects_unapproved_symbol_before_any_network_request() -> None:
    with pytest.raises(ValueError, match="approved ETF allowlist"):
        TwelveDataInstrument(
            instrument_id="ins_us_etf_iwm",
            canonical_symbol="ARCX:IWM",
            source_symbol="IWM",
        )


@pytest.mark.asyncio
async def test_GOV_026_twelve_data_binds_only_the_live_market_primary_role() -> None:
    provider = _provider(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    try:
        registry = ProviderRegistry()
        register_us_twelve_data_provider_roles(registry, provider)

        assert registry.resolve(TWELVE_DATA_PRIMARY_ROLE) is provider
        assert provider.capabilities().external_llm_allowed is False
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_PRV_001_factory_makes_fixture_and_live_selection_explicit_by_environment() -> None:
    fixture_registry = create_us_provider_registry(
        app_env="test",
        provider_mode="fixture",
    )
    try:
        for role in US_FIXTURE_CONTRACT_ROLE_BINDINGS:
            fixture_registry.resolve(role)
        with pytest.raises(ProviderRegistryError, match="not bound"):
            fixture_registry.resolve(TWELVE_DATA_PRIMARY_ROLE)
    finally:
        await fixture_registry.close()

    live_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    )
    live_registry = create_us_provider_registry(
        app_env="production",
        provider_mode="live",
        api_key=TEST_API_KEY,
        live_instruments=[SPY],
        cursor_signing_secret="unit-test-cursor-secret",
        client=live_client,
        clock=lambda: NOW,
    )
    try:
        assert live_registry.resolve(TWELVE_DATA_PRIMARY_ROLE).capabilities().provider_id == (
            "us.twelve-data.v1"
        )
    finally:
        await live_registry.close()
        await live_client.aclose()

    with pytest.raises(ValueError, match="production US provider mode must be live"):
        create_us_provider_registry(
            app_env="production",
            provider_mode="fixture",
        )


@pytest.mark.asyncio
async def test_PRV_001_factory_reads_live_secrets_from_runtime_settings() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    settings = Settings(
        app_env="production",
        service_token=SecretStr("test-service-token"),
        us_provider_mode="live",
        twelve_data_api_key=TEST_API_KEY,
        twelve_data_cursor_secret=SecretStr("unit-test-cursor-secret"),
    )
    registry = create_us_provider_registry_from_settings(
        settings,
        live_instruments=[SPY],
        client=client,
        clock=lambda: NOW,
    )
    try:
        assert registry.resolve(TWELVE_DATA_PRIMARY_ROLE).capabilities().provider_id == (
            "us.twelve-data.v1"
        )
    finally:
        await registry.close()
        await client.aclose()


@pytest.mark.parametrize(
    ("response", "error_type", "retryable", "retry_after_seconds"),
    [
        pytest.param(
            httpx.Response(401),
            ProviderAuthenticationError,
            False,
            None,
            id="PRV-008-http-401",
        ),
        pytest.param(
            httpx.Response(403),
            ProviderAuthorizationError,
            False,
            None,
            id="PRV-008-http-403",
        ),
        pytest.param(
            httpx.Response(429, headers={"Retry-After": "30"}),
            ProviderRateLimitError,
            True,
            30,
            id="PRV-007",
        ),
        pytest.param(
            httpx.Response(200, headers={"Content-Type": "text/html"}, text="<html>login</html>"),
            ProviderAuthorizationError,
            False,
            None,
            id="PRV-008-html-auth-wall",
        ),
        pytest.param(
            httpx.Response(200, content=b"not-json"),
            ProviderSchemaError,
            False,
            None,
            id="PRV-009-malformed-json",
        ),
        pytest.param(
            httpx.Response(200, json={"meta": {}, "values": {}}),
            ProviderSchemaError,
            False,
            None,
            id="PRV-020-schema-drift",
        ),
    ],
)
@pytest.mark.asyncio
async def test_PRV_008_009_020_maps_live_provider_failures(
    response: httpx.Response,
    error_type: type[ProviderError],
    retryable: bool,
    retry_after_seconds: int | None,
) -> None:
    provider = _provider(transport=httpx.MockTransport(lambda request: response))
    try:
        with pytest.raises(error_type) as error:
            await provider.fetch_bars(_bar_query(), CONTEXT)
    finally:
        await provider.aclose()

    provider_error = error.value
    assert provider_error.retryable is retryable
    assert provider_error.retry_after_seconds == retry_after_seconds


@pytest.mark.asyncio
async def test_PRV_007_retries_rate_limit_with_bounded_retry_after_delay() -> None:
    calls = 0
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, json=_success_payload(), request=request)

    provider = _provider(transport=httpx.MockTransport(handler), sleeper=sleep)
    try:
        page = await provider.fetch_bars(_bar_query(), CONTEXT)
    finally:
        await provider.aclose()

    assert [item.trading_date.isoformat() for item in page.items] == ["2026-07-22"]
    assert calls == 2
    assert delays == [0]


@pytest.mark.asyncio
async def test_PRV_007_retries_a_transport_timeout_once_before_success() -> None:
    calls = 0

    async def sleep(delay: float) -> None:
        assert delay >= 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json=_success_payload(), request=request)

    provider = _provider(transport=httpx.MockTransport(handler), sleeper=sleep)
    try:
        page = await provider.fetch_bars(_bar_query(), CONTEXT)
    finally:
        await provider.aclose()

    assert len(page.items) == 1
    assert calls == 2


@pytest.mark.asyncio
async def test_PIT_001_rejects_an_as_of_before_the_live_first_seen_time() -> None:
    provider = _provider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_success_payload(), request=request)
        )
    )
    query = _bar_query().model_copy(update={"as_of": NOW - timedelta(milliseconds=1)})
    try:
        with pytest.raises(UnsupportedCapabilityError, match="point-in-time"):
            await provider.fetch_bars(query, CONTEXT)
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_PRV_003_returns_two_complete_pages_without_duplicates_or_omissions() -> None:
    payload = _success_payload(
        values=[
            {
                "datetime": "2026-07-21",
                "open": "600.00",
                "high": "605.00",
                "low": "599.00",
                "close": "604.00",
                "volume": "100",
            },
            {
                "datetime": "2026-07-22",
                "open": "610.00",
                "high": "615.00",
                "low": "609.00",
                "close": "614.00",
                "volume": "200",
            },
        ]
    )
    provider = _provider(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    try:
        first = await provider.fetch_bars(
            _bar_query(limit=1).model_copy(
                update={"start": datetime(2026, 7, 21, 4, 0, tzinfo=UTC)}
            ),
            CONTEXT,
        )
        second = await provider.fetch_bars(
            _bar_query(limit=1, cursor=first.next_cursor).model_copy(
                update={"start": datetime(2026, 7, 21, 4, 0, tzinfo=UTC)}
            ),
            CONTEXT,
        )
    finally:
        await provider.aclose()

    assert first.complete is False
    assert first.next_cursor is not None
    assert second.complete is True
    assert second.next_cursor is None
    all_bar_ids = [item.bar_id for item in [*first.items, *second.items]]
    assert len(all_bar_ids) == len(set(all_bar_ids)) == 2
    assert [item.trading_date.isoformat() for item in [*first.items, *second.items]] == [
        "2026-07-21",
        "2026-07-22",
    ]


@pytest.mark.asyncio
async def test_PRV_003_rejects_duplicate_or_descending_provider_dates() -> None:
    provider = _provider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=_success_payload(
                    values=[
                        {
                            "datetime": "2026-07-22",
                            "open": "610",
                            "high": "615",
                            "low": "609",
                            "close": "614",
                            "volume": "1",
                        },
                        {
                            "datetime": "2026-07-22",
                            "open": "610",
                            "high": "615",
                            "low": "609",
                            "close": "614",
                            "volume": "1",
                        },
                    ]
                ),
            )
        )
    )
    try:
        with pytest.raises(ProviderCursorError, match="duplicate or descending"):
            await provider.fetch_bars(_bar_query(), CONTEXT)
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_PRV_010_maps_transport_timeout_without_treating_it_as_empty_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    provider = _provider(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderTimeoutError) as error:
            await provider.fetch_bars(_bar_query(), CONTEXT)
    finally:
        await provider.aclose()

    assert error.value.retryable is True


@pytest.mark.asyncio
async def test_PRV_001_requests_each_of_the_three_approved_market_proxies_only() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        symbol = request.url.params["symbol"]
        requests.append(symbol)
        return httpx.Response(
            200,
            json={
                "meta": {"symbol": symbol, "interval": "1day", "currency": "USD"},
                "values": [
                    {
                        "datetime": "2026-07-22",
                        "open": "610",
                        "high": "615",
                        "low": "609",
                        "close": "614",
                        "volume": "1",
                    }
                ],
            },
        )

    provider = _provider(
        transport=httpx.MockTransport(handler),
        instruments=[SPY, QQQ, DIA],
    )
    try:
        page = await provider.fetch_bars(
            _bar_query().model_copy(
                update={"instrument_ids": [SPY.instrument_id, QQQ.instrument_id, DIA.instrument_id]}
            ),
            CONTEXT,
        )
    finally:
        await provider.aclose()

    assert set(requests) == {"SPY", "QQQ", "DIA"}
    assert {bar.source.source_symbol for bar in page.items} == {"SPY", "QQQ", "DIA"}


@pytest.mark.asyncio
async def test_PRV_008_health_is_not_configured_without_an_api_key_and_never_calls_upstream() -> (
    None
):
    requested = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested = True
        return httpx.Response(200, json=_success_payload())

    provider = _provider(transport=httpx.MockTransport(handler), no_api_key=True)
    try:
        health = await provider.healthcheck()
    finally:
        await provider.aclose()

    assert health.status == "not_configured"
    assert requested is False


@pytest.mark.asyncio
async def test_PIT_009_rejects_historical_as_of_without_fetching_live_data() -> None:
    requested = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested = True
        return httpx.Response(200, json=_success_payload())

    provider = _provider(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UnsupportedCapabilityError, match="point-in-time"):
            await provider.fetch_bars(
                _bar_query().model_copy(update={"as_of": datetime(2026, 7, 22, tzinfo=UTC)}),
                CONTEXT,
            )
    finally:
        await provider.aclose()

    assert requested is False


@pytest.mark.asyncio
async def test_PRV_017_checksum_is_stable_for_reordered_json_and_changes_for_revised_ohlcv() -> (
    None
):
    payloads = [
        {
            "values": [
                {
                    "volume": "123456",
                    "close": "614.00",
                    "low": "609.00",
                    "datetime": "2026-07-22",
                    "high": "615.00",
                    "open": "610.00",
                }
            ],
            "meta": {"currency": "USD", "interval": "1day", "symbol": "SPY"},
        },
        _success_payload(),
        _success_payload(
            values=[
                {
                    "datetime": "2026-07-22",
                    "open": "610.00",
                    "high": "615.00",
                    "low": "609.00",
                    "close": "613.99",
                    "volume": "123456",
                }
            ]
        ),
    ]
    provider = _provider(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payloads.pop(0)))
    )
    try:
        first = await provider.fetch_bars(_bar_query(), CONTEXT)
        second = await provider.fetch_bars(_bar_query(), CONTEXT)
        revised = await provider.fetch_bars(_bar_query(), CONTEXT)
    finally:
        await provider.aclose()

    assert first.items[0].source.checksum_sha256 == second.items[0].source.checksum_sha256
    assert first.items[0].source.checksum_sha256 != revised.items[0].source.checksum_sha256


@pytest.mark.parametrize(
    ("instrument_id", "canonical_symbol", "source_symbol", "currency", "message"),
    [
        pytest.param("", "ARCX:SPY", "SPY", "USD", "instrument_id", id="empty-id"),
        pytest.param("ins", "spy", "SPY", "USD", "requires", id="canonical"),
        pytest.param("ins", "ARCX:SPY", "spy", "USD", "uppercase", id="source-case"),
        pytest.param("ins", "ARCX:SPY", "SPY", "EUR", "USD", id="currency"),
    ],
)
def test_GOV_026_validates_static_twelve_data_instrument_mapping(
    instrument_id: str,
    canonical_symbol: str,
    source_symbol: str,
    currency: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        TwelveDataInstrument(
            instrument_id=instrument_id,
            canonical_symbol=canonical_symbol,
            source_symbol=source_symbol,
            currency=currency,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_status", "expected_message"),
    [
        pytest.param(
            httpx.Response(200, json=_success_payload()),
            "ok",
            None,
            id="PRV-001-ok",
        ),
        pytest.param(
            httpx.Response(401),
            "not_configured",
            "ProviderAuthenticationError",
            id="PRV-008-auth",
        ),
        pytest.param(
            httpx.Response(503),
            "down",
            "ProviderUnavailableError",
            id="PRV-007-unavailable",
        ),
    ],
)
async def test_PRV_001_007_008_healthcheck_classifies_live_provider_status(
    response: httpx.Response,
    expected_status: str,
    expected_message: str | None,
) -> None:
    provider = _provider(transport=httpx.MockTransport(lambda request: response))
    try:
        health = await provider.healthcheck()
    finally:
        await provider.aclose()

    assert health.status == expected_status
    assert health.message == expected_message


@pytest.mark.asyncio
async def test_PRV_001_rejects_unsupported_query_and_unknown_instrument_before_returning_data() -> (
    None
):
    provider = _provider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_success_payload(), request=request)
        )
    )
    try:
        with pytest.raises(UnsupportedCapabilityError, match="raw daily"):
            await provider.fetch_bars(
                _bar_query().model_copy(update={"interval": Interval.W1}),
                CONTEXT,
            )
        with pytest.raises(UnsupportedCapabilityError, match="configured for instrument"):
            await provider.fetch_bars(
                _bar_query().model_copy(update={"instrument_ids": ["ins_unknown"]}),
                CONTEXT,
            )
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_PRV_001_handles_empty_business_window_without_an_upstream_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_success_payload(), request=request)

    provider = _provider(transport=httpx.MockTransport(handler))
    query = _bar_query().model_copy(
        update={
            "start": datetime(2026, 7, 24, 4, tzinfo=UTC),
            "end": datetime(2026, 7, 23, 4, tzinfo=UTC),
        }
    )
    try:
        page = await provider.fetch_bars(query, CONTEXT)
    finally:
        await provider.aclose()

    assert page.items == []
    assert page.complete is True
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        pytest.param({"meta": {}, "values": []}, "symbol", id="meta-symbol"),
        pytest.param(
            {"meta": {"symbol": "SPY", "interval": "1w", "currency": "USD"}, "values": []},
            "interval",
            id="meta-interval",
        ),
        pytest.param(
            {"meta": {"symbol": "SPY", "interval": "1day", "currency": "EUR"}, "values": []},
            "currency",
            id="meta-currency",
        ),
        pytest.param(
            {"meta": {"symbol": "SPY", "interval": "1day", "currency": "USD"}, "values": {}},
            "missing meta or values",
            id="values-shape",
        ),
    ],
)
async def test_PRV_020_rejects_schema_drift_before_normalization(
    payload: dict[str, object], message: str
) -> None:
    provider = _provider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload, request=request)
        )
    )
    try:
        with pytest.raises(ProviderSchemaError, match=message):
            await provider.fetch_bars(_bar_query(), CONTEXT)
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_PRV_007_stops_retrying_when_retry_after_exceeds_the_request_deadline() -> None:
    calls = 0

    async def must_not_sleep(_: float) -> None:
        raise AssertionError("a retry beyond the deadline must not sleep")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "30"}, request=request)

    provider = _provider(transport=httpx.MockTransport(handler), sleeper=must_not_sleep)
    tight_context = CONTEXT.model_copy(update={"deadline_at": NOW + timedelta(seconds=1)})
    try:
        with pytest.raises(ProviderRateLimitError):
            await provider.fetch_bars(_bar_query(), tight_context)
    finally:
        await provider.aclose()

    assert calls == 1


@pytest.mark.asyncio
async def test_PRV_001_rejects_non_bar_dataset_for_production_role() -> None:
    provider = _provider(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    try:
        with pytest.raises(UnsupportedCapabilityError, match="daily bars"):
            provider.assert_production_dataset_supported(Dataset.NEWS)
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_PRV_001_exposes_effective_dated_instrument_contracts_for_bar_storage() -> None:
    provider = _provider(
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        instruments=[SPY, QQQ, DIA],
    )
    try:
        instruments = provider.instrument_contracts(fetched_at=NOW)
    finally:
        await provider.aclose()

    assert [
        (item.local_symbol, item.venue_mic, item.valid_from.isoformat()) for item in instruments
    ] == [
        ("SPY", "ARCX", "1993-01-22"),
        ("QQQ", "XNAS", "1999-03-10"),
        ("DIA", "ARCX", "1998-01-14"),
    ]
    assert all(item.asset_class.value == "etf" for item in instruments)
    assert all(item.source.retrieved_at == NOW for item in instruments)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "error_type"),
    [
        pytest.param(401, ProviderAuthenticationError, id="PRV-008-payload-401"),
        pytest.param(403, ProviderAuthorizationError, id="PRV-008-payload-403"),
        pytest.param(429, ProviderRateLimitError, id="PRV-007-payload-429"),
        pytest.param(503, ProviderUnavailableError, id="PRV-007-payload-503"),
        pytest.param("api_key", ProviderAuthenticationError, id="PRV-008-payload-api-key"),
        pytest.param(
            "permission_denied", ProviderAuthorizationError, id="PRV-008-payload-permission"
        ),
        pytest.param("rate_limit", ProviderRateLimitError, id="PRV-007-payload-rate-limit"),
        pytest.param("unexpected", ProviderSchemaError, id="PRV-020-payload-unknown"),
    ],
)
async def test_PRV_007_008_020_classifies_twelve_data_error_payloads(
    code: int | str, error_type: type[ProviderError]
) -> None:
    payload = {"status": "error", "code": code, "message": "provider response"}
    provider = _provider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload, request=request)
        )
    )
    try:
        with pytest.raises(error_type):
            await provider.fetch_bars(_bar_query(), CONTEXT)
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_PRV_009_rejects_non_object_json_and_non_object_value_rows() -> None:
    payloads: list[object] = [
        ["not-an-object"],
        {"meta": {"symbol": "SPY", "interval": "1day", "currency": "USD"}, "values": ["bad-row"]},
    ]
    provider = _provider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payloads.pop(0), request=request)
        )
    )
    try:
        with pytest.raises(ProviderSchemaError, match="non-object JSON"):
            await provider.fetch_bars(_bar_query(), CONTEXT)
        with pytest.raises(ProviderSchemaError, match="contain objects"):
            await provider.fetch_bars(_bar_query(), CONTEXT)
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_PRV_009_quarantines_invalid_date_and_non_finite_ohlcv() -> None:
    provider = _provider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=_success_payload(
                    values=[
                        {
                            "datetime": "not-a-date",
                            "open": "610",
                            "high": "615",
                            "low": "609",
                            "close": "614",
                        },
                        {
                            "datetime": "2026-07-22",
                            "open": "NaN",
                            "high": "615",
                            "low": "609",
                            "close": "614",
                        },
                    ]
                ),
                request=request,
            )
        )
    )
    try:
        page = await provider.fetch_bars(_bar_query(), CONTEXT)
    finally:
        await provider.aclose()

    assert page.items == []
    assert [warning.code for warning in page.warnings] == [
        "PROVIDER_RECORD_QUARANTINED",
        "PROVIDER_RECORD_QUARANTINED",
    ]
