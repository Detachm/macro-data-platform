from __future__ import annotations

import os
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from macro_platform.contracts.common import Region
from macro_platform.contracts.macro import MacroReleaseQuery
from macro_platform.contracts.provider import FetchContext
from macro_platform.providers.hk.release_calendar import HkCenstatdReleaseCalendarProvider
from macro_platform.providers.us.release_calendar import UsOfficialReleaseCalendarProvider
from tests.contract.provider_suite import assert_page_contract, assert_page_provenance

_REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _require_live_smoke() -> None:
    if os.environ.get("RUN_LIVE_PROVIDER_SMOKE") != "1":
        pytest.skip("set RUN_LIVE_PROVIDER_SMOKE=1 to call official release-calendar sources")


@pytest.mark.live
@pytest.mark.asyncio
async def test_hk_and_us_calendars_validate_two_consecutive_report_dates() -> None:
    _require_live_smoke()
    now = datetime.now(UTC)
    context = FetchContext(
        request_id=uuid4(),
        as_of=now + timedelta(minutes=2),
        deadline_at=now + timedelta(seconds=90),
    )
    providers = (
        (Region.HK, HkCenstatdReleaseCalendarProvider(cursor_signing_secret="live-smoke")),
        (Region.US, UsOfficialReleaseCalendarProvider(cursor_signing_secret="live-smoke")),
    )
    first_report_date = now.astimezone(_REPORT_TIMEZONE).date()
    try:
        for region, provider in providers:
            for report_date in (first_report_date, first_report_date + timedelta(days=1)):
                start = datetime.combine(report_date, time.min, _REPORT_TIMEZONE).astimezone(UTC)
                page = await provider.fetch_macro_releases(
                    MacroReleaseQuery(
                        regions={region},
                        scheduled_from=start,
                        scheduled_to=start + timedelta(days=8),
                        as_of=context.as_of,
                        limit=1000,
                    ),
                    context,
                )
                assert_page_contract(page)
                assert_page_provenance(page, provider.provider_id)
                assert page.complete is True
                assert page.source_watermark
                assert all(item.region is region for item in page.items)
    finally:
        for _, provider in providers:
            await provider.aclose()
