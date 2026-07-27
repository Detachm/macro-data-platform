from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from macro_platform.config import Settings
from macro_platform.contracts.market import Adjustment, BarQuery, Interval
from macro_platform.contracts.provider import FetchContext
from macro_platform.governance.source_policy import (
    NonProductionSourcePolicy,
    SourcePolicyDeniedError,
    load_production_source_policy,
)
from macro_platform.providers.base import (
    ProviderAuthenticationError,
    ProviderAuthorizationError,
    ProviderCursorError,
    ProviderError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderTimeoutError,
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


def _provider(
    *,
    transport: httpx.AsyncBaseTransport,
    api_key: SecretStr = TEST_API_KEY,
    no_api_key: bool = False,
    instruments: list[TwelveDataInstrument] | None = None,
) -> TwelveDataDailyBarsProvider:
    return TwelveDataDailyBarsProvider(
        api_key=None if no_api_key else api_key,
        instruments=instruments or [SPY],
        source_policy=load_production_source_policy(),
        client=httpx.AsyncClient(transport=transport),
        cursor_signing_secret="unit-test-cursor-secret",
        clock=lambda: NOW,
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
async def test_PRV_001_fetch_bars_maps_raw_daily_bar_and_redacts_api_key_from_provenance() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(
            "https://api.twelvedata.com/time_series?"
            "symbol=SPY&interval=1day&start_date=2026-07-22&end_date=2026-07-22&"
            "outputsize=5000&order=ASC&apikey=unit-test-api-key"
        )
        return httpx.Response(
            200,
            json=_success_payload(),
        )

    provider = _provider(transport=httpx.MockTransport(handler))
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
    assert bar.available_at == NOW
    assert bar.source.retrieved_at == NOW


def test_GOV_026_twelve_data_rejects_unapproved_symbol_before_any_network_request() -> None:
    with pytest.raises(SourcePolicyDeniedError, match="source symbol is not allowed"):
        TwelveDataDailyBarsProvider(
            api_key=SecretStr("unit-test-api-key"),
            instruments=[
                TwelveDataInstrument(
                    instrument_id="ins_us_etf_iwm",
                    canonical_symbol="ARCX:IWM",
                    source_symbol="IWM",
                )
            ],
            source_policy=load_production_source_policy(),
            cursor_signing_secret="unit-test-cursor-secret",
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
        source_policy=load_production_source_policy(),
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
        source_policy=load_production_source_policy(),
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
            source_policy=load_production_source_policy(),
        )

    with pytest.raises(ValueError, match="requires an enforced source policy"):
        create_us_provider_registry(
            app_env="development",
            provider_mode="live",
            source_policy=NonProductionSourcePolicy(),
            api_key=TEST_API_KEY,
            live_instruments=[SPY],
            cursor_signing_secret="unit-test-cursor-secret",
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
        source_policy=load_production_source_policy(),
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
