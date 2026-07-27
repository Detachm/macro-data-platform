from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from macro_platform.config import Settings
from macro_platform.contracts.common import Region
from macro_platform.contracts.market import Adjustment, BarQuery, Interval
from macro_platform.contracts.provider import FetchContext
from macro_platform.providers.base import (
    ProviderCursorError,
    ProviderTimeoutError,
    UnsupportedCapabilityError,
)
from macro_platform.providers.cn.baostock import (
    BAOSTOCK_PRIMARY_ROLE,
    BaoStockDailyBarsProvider,
    BaoStockInstrument,
    register_cn_baostock_provider_roles,
)
from macro_platform.providers.factory import create_provider_registry
from macro_platform.providers.registry import ProviderRegistry

NOW = datetime(2026, 7, 23, 21, 0, tzinfo=UTC)
CONTEXT = FetchContext(
    request_id=UUID("00000000-0000-4000-8000-000000000028"),
    as_of=NOW,
    deadline_at=datetime(2026, 7, 23, 21, 1, tzinfo=UTC),
)
CSI300 = BaoStockInstrument(
    instrument_id="ins_cn_index_csi300",
    canonical_symbol="XSHG:000300",
    source_symbol="sh.000300",
)
FIELDS = [
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "pctChg",
]


class _Result:
    def __init__(
        self,
        rows: list[list[str]] | None = None,
        *,
        error_code: str = "0",
        error_msg: str = "success",
        fields: list[str] | None = None,
    ) -> None:
        self.error_code = error_code
        self.error_msg = error_msg
        self.fields = FIELDS if fields is None else fields
        self._rows = [] if rows is None else rows
        self._index = 0

    def next(self) -> bool:
        if self._index >= len(self._rows):
            return False
        self._index += 1
        return True

    def get_row_data(self) -> list[str]:
        return self._rows[self._index - 1]


class _Client:
    def __init__(
        self,
        *,
        rows: list[list[str]] | None = None,
        login: _Result | None = None,
        query: _Result | None = None,
    ) -> None:
        self._rows = [] if rows is None else rows
        self._login = login or _Result()
        self._query = query
        self.calls: list[tuple[object, ...]] = []
        self.logouts = 0

    def login(self) -> _Result:
        self.calls.append(("login",))
        return self._login

    def logout(self) -> _Result:
        self.logouts += 1
        return _Result()

    def query_history_k_data_plus(
        self,
        code: str,
        fields: str,
        *,
        start_date: str,
        end_date: str,
        frequency: str,
        adjustflag: str,
    ) -> _Result:
        self.calls.append((code, fields, start_date, end_date, frequency, adjustflag))
        return self._query or _Result([*self._rows])


def _provider(client: _Client) -> BaoStockDailyBarsProvider:
    return BaoStockDailyBarsProvider(
        instruments=[CSI300],
        client=client,
        cursor_signing_secret="unit-test-cursor-secret",
        clock=lambda: NOW,
    )


def _query(*, limit: int = 1000, cursor: str | None = None) -> BarQuery:
    return BarQuery(
        instrument_ids=[CSI300.instrument_id],
        interval=Interval.D1,
        start=datetime(2026, 7, 23, tzinfo=UTC),
        end=datetime(2026, 7, 24, tzinfo=UTC),
        adjustment=Adjustment.RAW,
        as_of=NOW,
        cursor=cursor,
        limit=limit,
    )


def _row(
    *,
    trading_date: str = "2026-07-23",
    low: str = "4650.0",
    high: str = "4700.0",
) -> list[str]:
    return [
        trading_date,
        "sh.000300",
        "4680.0",
        high,
        low,
        "4690.0",
        "4670.0",
        "123456",
        "789012.5",
        "0.43",
    ]


@pytest.mark.asyncio
async def test_baostock_maps_raw_cn_daily_bar_with_bounded_public_query() -> None:
    client = _Client(rows=[_row()])
    provider = _provider(client)

    page = await provider.fetch_bars(_query(), CONTEXT)

    assert page.complete is True
    assert len(page.items) == 1
    bar = page.items[0]
    assert bar.canonical_symbol == "XSHG:000300"
    assert bar.region is Region.CN
    assert bar.bar_start == datetime(2026, 7, 23, 1, 30, tzinfo=UTC)
    assert bar.bar_end == datetime(2026, 7, 23, 7, 0, tzinfo=UTC)
    assert bar.source.provider_record_id == "cn.baostock.v1:sh.000300:1d:2026-07-23:raw"
    assert str(bar.source.source_url) == "https://www.baostock.com/"
    assert client.calls[1] == (
        "sh.000300",
        "date,code,open,high,low,close,preclose,volume,amount,pctChg",
        "2026-07-23",
        "2026-07-24",
        "d",
        "3",
    )
    assert client.logouts == 1


def test_baostock_rejects_unapproved_core_index_mapping() -> None:
    with pytest.raises(ValueError, match="approved core-index allowlist"):
        BaoStockInstrument(
            instrument_id="ins_cn_index_unknown",
            canonical_symbol="XSHG:000905",
            source_symbol="sh.000905",
        )


@pytest.mark.asyncio
async def test_baostock_quarantines_malformed_record_without_discarding_page() -> None:
    client = _Client(rows=[_row(low="4800.0", high="4700.0")])
    provider = _provider(client)

    page = await provider.fetch_bars(_query(), CONTEXT)

    assert page.items == []
    assert page.warnings[0].code == "PROVIDER_RECORD_QUARANTINED"
    assert page.warnings[0].details["rejection"]["error_code"] == "SCHEMA_DRIFT"


@pytest.mark.asyncio
async def test_baostock_detects_changed_snapshot_during_local_pagination() -> None:
    first_client = _Client(rows=[_row()])
    provider = _provider(first_client)
    first = await provider.fetch_bars(_query(limit=1), CONTEXT)
    assert first.next_cursor is None

    client = _Client(rows=[_row(), _row(trading_date="2026-07-24")])
    provider = _provider(client)
    first = await provider.fetch_bars(
        _query(limit=1).model_copy(
            update={"end": datetime(2026, 7, 25, tzinfo=UTC)},
        ),
        CONTEXT,
    )
    assert first.next_cursor is not None
    client._rows[-1][-2] = "999999.9"
    with pytest.raises(ProviderCursorError, match="source changed"):
        await provider.fetch_bars(
            _query(limit=1, cursor=first.next_cursor).model_copy(
                update={"end": datetime(2026, 7, 25, tzinfo=UTC)},
            ),
            CONTEXT,
        )


@pytest.mark.asyncio
async def test_baostock_classifies_login_timeout_and_rejects_historical_pit() -> None:
    timeout_provider = _provider(_Client(login=_Result(error_code="100", error_msg="连接超时")))
    with pytest.raises(ProviderTimeoutError):
        await timeout_provider.fetch_bars(_query(), CONTEXT)

    provider = _provider(_Client(rows=[_row()]))
    with pytest.raises(UnsupportedCapabilityError, match="historical point-in-time"):
        await provider.fetch_bars(
            _query().model_copy(update={"as_of": datetime(2026, 7, 22, tzinfo=UTC)}),
            CONTEXT,
        )


@pytest.mark.asyncio
async def test_baostock_binds_live_cn_bars_role_in_factory() -> None:
    registry = ProviderRegistry()
    provider = _provider(_Client(rows=[_row()]))
    register_cn_baostock_provider_roles(registry, provider)
    assert registry.resolve(BAOSTOCK_PRIMARY_ROLE) is provider

    live_registry = create_provider_registry(
        Settings(app_env="test", provider_mode="live", provider_cursor_secret="test-cursor")
    )
    try:
        assert (
            live_registry.resolve(BAOSTOCK_PRIMARY_ROLE).capabilities().provider_id
            == "cn.baostock.v1"
        )
    finally:
        await live_registry.close()
