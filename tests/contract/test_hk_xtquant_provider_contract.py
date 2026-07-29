from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from macro_platform.contracts.common import Region
from macro_platform.contracts.market import Adjustment, BarQuery, Interval
from macro_platform.contracts.provider import Dataset, FetchContext
from macro_platform.providers.hk.xtquant import (
    HK_XTQUANT_PROVIDER_ID,
    HkXtQuantDailyBarsProvider,
    HkXtQuantInstrument,
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
TENCENT = HkXtQuantInstrument(
    instrument_id="ins_hk_equity_00700",
    canonical_symbol="XHKG:00700",
    source_symbol="00700.HK",
)


class _Frame:
    def reset_index(self) -> _Frame:
        return self

    def to_dict(self, *, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return [
            {
                "index": "20260723",
                "time": int(datetime(2026, 7, 23, tzinfo=UTC).timestamp() * 1000),
                "open": 468.0,
                "high": 470.0,
                "low": 465.0,
                "close": 469.0,
                "volume": 123456.0,
                "amount": 57_890_123.5,
                "preClose": 467.0,
            }
        ]


class _Client:
    def connect(self, *_: object, **__: object) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def download_history_data2(self, *_: object, **__: object) -> None:
        return None

    def get_market_data_ex(self, **_: object) -> dict[str, _Frame]:
        return {"00700.HK": _Frame()}


@pytest.mark.asyncio
async def test_PRV_001_PRV_012_PRV_017_xtquant_daily_bars_meet_shared_provider_contract() -> None:
    provider = HkXtQuantDailyBarsProvider(
        instruments=[TENCENT],
        client=_Client(),
        cursor_signing_secret="contract-test-cursor-secret",
        clock=lambda: NOW,
    )
    try:
        capabilities = assert_capabilities_contract(provider)
        page = await provider.fetch_bars(
            BarQuery(
                instrument_ids=[TENCENT.instrument_id],
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

    assert capabilities.provider_id == HK_XTQUANT_PROVIDER_ID
    assert capabilities.regions == {Region.HK}
    assert capabilities.datasets == {Dataset.BARS}
    assert capabilities.intervals == {Interval.D1}
    assert capabilities.supports_point_in_time is False
    assert capabilities.external_llm_allowed is False
    assert_page_contract(page)
    assert_page_provenance(page, HK_XTQUANT_PROVIDER_ID)
    assert_available_at_not_after_as_of(page, as_of=NOW)
    assert page.complete is True
    assert [item.trading_date.isoformat() for item in page.items] == ["2026-07-23"]
