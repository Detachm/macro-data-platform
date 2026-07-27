from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from macro_platform.contracts.common import Region
from macro_platform.contracts.market import Adjustment, BarQuery, Interval
from macro_platform.contracts.provider import Dataset, FetchContext
from macro_platform.providers.cn.baostock import (
    BAOSTOCK_PROVIDER_ID,
    BaoStockDailyBarsProvider,
    BaoStockInstrument,
)
from tests.contract.provider_suite import (
    assert_available_at_not_after_as_of,
    assert_capabilities_contract,
    assert_page_contract,
    assert_page_provenance,
)

NOW = datetime(2026, 7, 23, 21, tzinfo=UTC)
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
    error_code = "0"
    error_msg = "success"
    fields = FIELDS

    def __init__(self) -> None:
        self._rows = [
            [
                "2026-07-23",
                "sh.000300",
                "4680.0",
                "4700.0",
                "4650.0",
                "4690.0",
                "4670.0",
                "123456",
                "789012.5",
                "0.43",
            ]
        ]
        self._index = 0

    def next(self) -> bool:
        if self._index >= len(self._rows):
            return False
        self._index += 1
        return True

    def get_row_data(self) -> list[str]:
        return self._rows[self._index - 1]


class _Client:
    def login(self) -> _Result:
        return _Result()

    def logout(self) -> _Result:
        return _Result()

    def query_history_k_data_plus(self, *_: object, **__: object) -> _Result:
        return _Result()


@pytest.mark.asyncio
async def test_PRV_001_PRV_012_PRV_017_baostock_daily_bars_meet_shared_provider_contract() -> None:
    provider = BaoStockDailyBarsProvider(
        instruments=[CSI300],
        client=_Client(),
        cursor_signing_secret="contract-test-cursor-secret",
        clock=lambda: NOW,
    )
    try:
        capabilities = assert_capabilities_contract(provider)
        page = await provider.fetch_bars(
            BarQuery(
                instrument_ids=[CSI300.instrument_id],
                interval=Interval.D1,
                start=datetime(2026, 7, 22, 16, tzinfo=UTC),
                end=datetime(2026, 7, 23, 16, tzinfo=UTC),
                adjustment=Adjustment.RAW,
                as_of=NOW,
                limit=10,
            ),
            CONTEXT,
        )
    finally:
        await provider.aclose()

    assert capabilities.provider_id == BAOSTOCK_PROVIDER_ID
    assert capabilities.regions == {Region.CN}
    assert capabilities.datasets == {Dataset.BARS}
    assert capabilities.intervals == {Interval.D1}
    assert capabilities.supports_point_in_time is False
    assert capabilities.external_llm_allowed is False
    assert_page_contract(page)
    assert_page_provenance(page, BAOSTOCK_PROVIDER_ID)
    assert_available_at_not_after_as_of(page, as_of=NOW)
    assert page.complete is True
    assert [item.trading_date.isoformat() for item in page.items] == ["2026-07-23"]
