from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from macro_platform.config import Settings
from macro_platform.contracts.common import AssetClass, Region
from macro_platform.contracts.market import Adjustment, BarQuery, Interval
from macro_platform.contracts.provider import FetchContext
from macro_platform.providers.base import ProviderCursorError, ProviderUnavailableError
from macro_platform.providers.factory import create_provider_registry
from macro_platform.providers.hk.xtquant import (
    HK_XTQUANT_CORE_INDEX_INSTRUMENTS,
    HK_XTQUANT_EQUITY_ROLE,
    HK_XTQUANT_PRIMARY_ROLE,
    HkXtQuantDailyBarsProvider,
    HkXtQuantInstrument,
    register_hk_xtquant_provider_roles,
)
from macro_platform.providers.registry import ProviderRegistry

NOW = datetime(2026, 7, 23, 21, tzinfo=UTC)
CONTEXT = FetchContext(
    request_id=UUID("00000000-0000-4000-8000-000000000028"),
    as_of=NOW,
    deadline_at=datetime(2026, 7, 23, 21, 1, tzinfo=UTC),
)
TENCENT = HkXtQuantInstrument(
    instrument_id="ins_hk_equity_00700",
    canonical_symbol="XHKG:00700",
    source_symbol="00700.HK",
)


class _Frame:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records

    def reset_index(self) -> _Frame:
        return self

    def to_dict(self, *, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return self._records


class _Client:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records
        self.calls: list[tuple[object, ...]] = []

    def connect(
        self, ip: str = "", port: int | None = None, remember_if_success: bool = True
    ) -> None:
        self.calls.append(("connect", ip, port, remember_if_success))

    def download_history_data2(
        self,
        stock_list: list[str],
        period: str,
        start_time: str = "",
        end_time: str = "",
        callback: object | None = None,
    ) -> None:
        self.calls.append(("download", stock_list, period, start_time, end_time, callback))

    def get_market_data_ex(self, **kwargs: object) -> dict[str, _Frame]:
        self.calls.append(("read", kwargs))
        stock_list = kwargs["stock_list"]
        assert isinstance(stock_list, list)
        return {str(symbol): _Frame(self._records) for symbol in stock_list}

    def disconnect(self) -> None:
        self.calls.append(("disconnect",))


def _provider(client: _Client | None = None) -> HkXtQuantDailyBarsProvider:
    return HkXtQuantDailyBarsProvider(
        instruments=[TENCENT],
        client=client,
        host="127.0.0.1",
        port=58615,
        cursor_signing_secret="unit-test-cursor-secret",
        clock=lambda: NOW,
    )


def _query(*, limit: int = 1000, cursor: str | None = None) -> BarQuery:
    return BarQuery(
        instrument_ids=[TENCENT.instrument_id],
        interval=Interval.D1,
        start=datetime(2026, 7, 22, 16, tzinfo=UTC),
        end=datetime(2026, 7, 23, 16, tzinfo=UTC),
        adjustment=Adjustment.RAW,
        as_of=NOW,
        cursor=cursor,
        limit=limit,
    )


def _row(
    *,
    date_value: str = "20260723",
    low: float = 465.0,
    high: float = 470.0,
) -> dict[str, object]:
    day = datetime.strptime(date_value, "%Y%m%d").replace(tzinfo=UTC)
    return {
        "index": date_value,
        "time": int(day.timestamp() * 1000),
        "open": 468.0,
        "high": high,
        "low": low,
        "close": 469.0,
        "volume": 123456.0,
        "amount": 57_890_123.5,
        "preClose": 467.0,
        "suspendFlag": 0,
    }


@pytest.mark.asyncio
async def test_xtquant_maps_hk_daily_bar_via_shared_datacentre_protocol() -> None:
    client = _Client([_row()])
    provider = _provider(client)

    page = await provider.fetch_bars(_query(), CONTEXT)

    assert page.complete is True
    assert len(page.items) == 1
    bar = page.items[0]
    assert bar.canonical_symbol == "XHKG:00700"
    assert bar.region is Region.HK
    assert bar.bar_start == datetime(2026, 7, 23, 1, 30, tzinfo=UTC)
    assert bar.bar_end == datetime(2026, 7, 23, 8, tzinfo=UTC)
    assert bar.source.provider_record_id == "hk.xtquant.v1:00700.HK:1d:2026-07-23:raw"
    assert bar.source.source_url is None
    assert client.calls == [
        ("connect", "127.0.0.1", 58615, True),
        ("download", ["00700.HK"], "1d", "20260723", "20260723", None),
        (
            "read",
            {
                "field_list": [],
                "stock_list": ["00700.HK"],
                "period": "1d",
                "start_time": "20260723",
                "end_time": "20260723",
                "count": -1,
                "dividend_type": "none",
                "fill_data": False,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_xtquant_uses_hk_midnight_when_vendor_index_is_the_prior_utc_date() -> None:
    row = _row(date_value="20260722")
    row["time"] = int(datetime(2026, 7, 22, 16, tzinfo=UTC).timestamp() * 1000)

    page = await _provider(_Client([row])).fetch_bars(_query(), CONTEXT)

    assert len(page.items) == 1
    assert page.items[0].trading_date == date(2026, 7, 23)
    assert page.items[0].bar_start == datetime(2026, 7, 23, 1, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_xtquant_still_quarantines_unrecognized_date_time_disagreement() -> None:
    row = _row(date_value="20260721")
    row["time"] = int(datetime(2026, 7, 22, 16, tzinfo=UTC).timestamp() * 1000)

    page = await _provider(_Client([row])).fetch_bars(_query(), CONTEXT)

    assert page.items == []
    assert page.warnings[0].code == "PROVIDER_RECORD_QUARANTINED"
    assert page.warnings[0].details["rejection"]["error_code"] == "PROVIDER_SCHEMA_CHANGED"


@pytest.mark.asyncio
async def test_xtquant_provider_disconnects_its_rpc_client_idempotently() -> None:
    client = _Client([_row()])
    provider = _provider(client)

    await provider.fetch_bars(_query(), CONTEXT)
    await provider.aclose()
    await provider.aclose()

    assert client.calls.count(("disconnect",)) == 1


@pytest.mark.asyncio
async def test_xtquant_maps_entitlement_confirmed_hk_core_indices() -> None:
    client = _Client([_row()])
    provider = HkXtQuantDailyBarsProvider(
        instruments=HK_XTQUANT_CORE_INDEX_INSTRUMENTS,
        client=client,
        host="127.0.0.1",
        port=58615,
        cursor_signing_secret="unit-test-cursor-secret",
        clock=lambda: NOW,
    )
    instrument_ids = [instrument.instrument_id for instrument in HK_XTQUANT_CORE_INDEX_INSTRUMENTS]

    page = await provider.fetch_bars(
        _query().model_copy(update={"instrument_ids": instrument_ids}), CONTEXT
    )
    contracts = provider.instrument_contracts(
        fetched_at=NOW,
        instrument_ids=instrument_ids,
    )

    assert {item.source.source_symbol for item in page.items} == {
        "HSI.HK",
        "HSCEI.HK",
        "HSTECH.HK",
    }
    assert {item.canonical_symbol for item in page.items} == {
        "XHKG:HSI",
        "XHKG:HSCEI",
        "XHKG:HSTECH",
    }
    assert {item.instrument_id for item in page.items} == {
        "ins_hk_index_hsi",
        "ins_hk_index_hscei",
        "ins_hk_index_hstech",
    }
    assert {item.asset_class for item in contracts} == {AssetClass.INDEX}
    assert {item.listed_on for item in contracts} == {
        date(1969, 11, 24),
        date(1994, 8, 8),
        date(2020, 7, 27),
    }


def test_xtquant_rejects_unapproved_hk_symbol() -> None:
    with pytest.raises(ValueError, match="approved HK allowlist"):
        HkXtQuantInstrument(
            instrument_id="ins_hk_equity_00001",
            canonical_symbol="XHKG:00001",
            source_symbol="00001.HK",
        )


@pytest.mark.asyncio
async def test_xtquant_quarantines_invalid_ohlc_without_persisting_raw_sdk_row() -> None:
    page = await _provider(_Client([_row(low=480.0, high=470.0)])).fetch_bars(_query(), CONTEXT)

    assert page.items == []
    rejection = page.warnings[0].details["rejection"]
    assert page.warnings[0].code == "PROVIDER_RECORD_QUARANTINED"
    assert rejection["error_code"] == "NORMALIZATION_ERROR"
    assert rejection["redacted_payload"] == {
        "date": "20260723",
        "time": int(datetime(2026, 7, 23, tzinfo=UTC).timestamp() * 1000),
        "fields": [
            "amount",
            "close",
            "high",
            "index",
            "low",
            "open",
            "preClose",
            "suspendFlag",
            "time",
            "volume",
        ],
    }


@pytest.mark.asyncio
async def test_xtquant_detects_changed_snapshot_during_local_pagination() -> None:
    client = _Client([_row(), _row(date_value="20260724")])
    provider = _provider(client)
    query = _query(limit=1).model_copy(update={"end": datetime(2026, 7, 24, 16, tzinfo=UTC)})
    first = await provider.fetch_bars(query, CONTEXT)
    assert first.next_cursor is not None
    client._records[-1]["amount"] = 99_999_999.0

    with pytest.raises(ProviderCursorError, match="source changed"):
        await provider.fetch_bars(query.model_copy(update={"cursor": first.next_cursor}), CONTEXT)


@pytest.mark.asyncio
async def test_xtquant_reports_missing_vendor_runtime_as_unavailable() -> None:
    provider = _provider()
    with pytest.raises(ProviderUnavailableError, match="runtime is not installed"):
        await provider.fetch_bars(_query(), CONTEXT)


@pytest.mark.asyncio
async def test_xtquant_binds_live_hk_bars_role_in_factory() -> None:
    provider = _provider(_Client([_row()]))
    registry = ProviderRegistry()
    register_hk_xtquant_provider_roles(registry, provider)
    assert registry.resolve(HK_XTQUANT_PRIMARY_ROLE) is provider
    assert registry.resolve(HK_XTQUANT_EQUITY_ROLE) is provider

    live_registry = create_provider_registry(
        Settings(app_env="test", provider_mode="live", provider_cursor_secret="test-cursor")
    )
    try:
        assert (
            live_registry.resolve(HK_XTQUANT_PRIMARY_ROLE).capabilities().provider_id
            == "hk.xtquant.v1"
        )
    finally:
        await live_registry.close()
