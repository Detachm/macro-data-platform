from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest

from macro_platform.contracts.common import Region
from macro_platform.contracts.macro import MacroObservationQuery, MacroReleaseQuery
from macro_platform.contracts.market import Adjustment, BarQuery, Interval
from macro_platform.contracts.news import NewsQuery
from macro_platform.contracts.provider import FetchContext
from macro_platform.providers.cn.baostock import (
    BAOSTOCK_DEFAULT_INSTRUMENTS,
    BaoStockDailyBarsProvider,
)
from macro_platform.providers.cn.live import CnNbsReleaseProvider
from macro_platform.providers.hk.live import HkCsdProvider, HkmaPressReleaseProvider
from macro_platform.providers.hk.xtquant import (
    HK_XTQUANT_DEFAULT_INSTRUMENTS,
    HkXtQuantDailyBarsProvider,
)


def _live_context() -> FetchContext:
    now = datetime.now(UTC)
    return FetchContext(
        request_id=UUID("00000000-0000-4000-8000-000000000028"),
        # Public providers return a server response after the request starts;
        # the small cushion prevents a valid current snapshot being treated as
        # an unsupported historical as-of query.
        as_of=now + timedelta(minutes=1),
        deadline_at=now + timedelta(seconds=30),
    )


def _require_live_smoke() -> None:
    if os.getenv("RUN_LIVE_SMOKE") != "1":
        pytest.skip("set RUN_LIVE_SMOKE=1 to call the real public provider")


def _require_xtquant_live_smoke() -> None:
    _require_live_smoke()
    if os.getenv("RUN_XTQUANT_LIVE_SMOKE") != "1":
        pytest.skip("set RUN_XTQUANT_LIVE_SMOKE=1 after configuring the shared XtQuant data centre")


@pytest.mark.live
@pytest.mark.asyncio
async def test_cn_baostock_core_index_daily_bar_live_smoke() -> None:
    _require_live_smoke()
    context = _live_context()
    csi300 = BAOSTOCK_DEFAULT_INSTRUMENTS[1]
    provider = BaoStockDailyBarsProvider(
        instruments=[csi300],
        cursor_signing_secret="live-smoke-only-cursor-secret",
    )
    try:
        page = await provider.fetch_bars(
            BarQuery(
                instrument_ids=[csi300.instrument_id],
                interval=Interval.D1,
                start=context.as_of - timedelta(days=14),
                end=context.as_of + timedelta(days=1),
                adjustment=Adjustment.RAW,
                as_of=context.as_of,
                limit=20,
            ),
            context,
        )
    finally:
        await provider.aclose()

    assert page.items
    assert {bar.source.source_symbol for bar in page.items} == {"sh.000300"}
    assert all(bar.interval is Interval.D1 for bar in page.items)


@pytest.mark.live
@pytest.mark.asyncio
async def test_hk_xtquant_tencent_daily_bar_live_smoke() -> None:
    _require_xtquant_live_smoke()
    context = _live_context()
    tencent = HK_XTQUANT_DEFAULT_INSTRUMENTS[0]
    provider = HkXtQuantDailyBarsProvider(
        instruments=[tencent],
        host=os.getenv("HK_XTQUANT_HOST", "127.0.0.1"),
        port=int(os.getenv("HK_XTQUANT_PORT", "58615")),
        cursor_signing_secret="live-smoke-only-cursor-secret",
    )
    try:
        page = await provider.fetch_bars(
            BarQuery(
                instrument_ids=[tencent.instrument_id],
                interval=Interval.D1,
                start=context.as_of - timedelta(days=14),
                end=context.as_of + timedelta(days=1),
                adjustment=Adjustment.RAW,
                as_of=context.as_of,
                limit=20,
            ),
            context,
        )
    finally:
        await provider.aclose()

    assert page.items
    assert {bar.source.source_symbol for bar in page.items} == {"00700.HK"}
    assert all(bar.interval is Interval.D1 for bar in page.items)


@pytest.mark.live
@pytest.mark.asyncio
async def test_cn_nbs_release_calendar_live_smoke() -> None:
    _require_live_smoke()
    context = _live_context()
    provider = CnNbsReleaseProvider()
    try:
        page = await provider.fetch_macro_releases(
            MacroReleaseQuery(
                regions={Region.CN},
                scheduled_from=datetime(2020, 1, 1, tzinfo=UTC),
                scheduled_to=datetime(2030, 1, 1, tzinfo=UTC),
                as_of=context.as_of,
                limit=10,
            ),
            context,
        )
        assert page.source_watermark
        assert page.items
    finally:
        await provider.aclose()


@pytest.mark.live
@pytest.mark.asyncio
async def test_hk_csd_live_smoke() -> None:
    _require_live_smoke()
    context = _live_context()
    provider = HkCsdProvider()
    try:
        page = await provider.fetch_macro_observations(
            MacroObservationQuery(
                series_ids=["macro:HK:CENSTATD:510-60004:SCC_CM"],
                period_from=date(2000, 1, 1),
                period_to=date(2030, 12, 31),
                as_of=context.as_of,
                limit=10,
            ),
            context,
        )
        assert page.source_watermark
        assert all(item.series_id == "macro:HK:CENSTATD:510-60004:SCC_CM" for item in page.items)
    finally:
        await provider.aclose()


@pytest.mark.live
@pytest.mark.asyncio
async def test_hkma_press_release_live_smoke() -> None:
    _require_live_smoke()
    context = _live_context()
    provider = HkmaPressReleaseProvider()
    try:
        page = await provider.fetch_news(
            NewsQuery(
                regions={Region.HK},
                published_from=datetime(2000, 1, 1, tzinfo=UTC),
                published_to=datetime(2030, 1, 1, tzinfo=UTC),
                as_of=context.as_of,
                limit=10,
            ),
            context,
        )
        assert page.source_watermark
        assert all(item.time_precision == "date" for item in page.items)
    finally:
        await provider.aclose()
