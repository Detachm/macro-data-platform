from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr

from macro_platform.contracts.market import Adjustment, BarQuery, Interval
from macro_platform.contracts.provider import FetchContext
from macro_platform.providers.us.twelve_data import (
    TWELVE_DATA_DEFAULT_INSTRUMENTS,
    TwelveDataDailyBarsProvider,
)


@pytest.mark.live
@pytest.mark.asyncio
async def test_PRV_001_twelve_data_basic_spy_daily_bar_smoke() -> None:
    api_key = os.environ.get("TWELVE_DATA_API_KEY")
    cursor_secret = os.environ.get("TWELVE_DATA_CURSOR_SECRET")
    if not api_key or not cursor_secret:
        pytest.skip("TWELVE_DATA_API_KEY and TWELVE_DATA_CURSOR_SECRET are required for live smoke")

    now = datetime.now(tz=UTC)
    spy = TWELVE_DATA_DEFAULT_INSTRUMENTS[0]
    provider = TwelveDataDailyBarsProvider(
        api_key=SecretStr(api_key),
        instruments=[spy],
        cursor_signing_secret=cursor_secret,
    )
    try:
        deadline_at = now + timedelta(seconds=provider.request_timeout_seconds)
        page = await provider.fetch_bars(
            BarQuery(
                instrument_ids=[spy.instrument_id],
                interval=Interval.D1,
                start=now - timedelta(days=10),
                end=now,
                adjustment=Adjustment.RAW,
                as_of=deadline_at,
                limit=10,
            ),
            FetchContext(
                request_id=uuid4(),
                as_of=deadline_at,
                deadline_at=deadline_at,
            ),
        )
    finally:
        await provider.aclose()

    assert page.items
    assert {bar.source.source_symbol for bar in page.items} == {"SPY"}
    assert all(bar.interval is Interval.D1 for bar in page.items)
    assert all("apikey" not in str(bar.source.source_url) for bar in page.items)
