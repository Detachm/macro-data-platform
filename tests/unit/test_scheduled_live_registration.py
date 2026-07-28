from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from macro_platform.config import Settings
from macro_platform.contracts.provider import Dataset
from macro_platform.jobs.scheduler import build_registered_tasks
from macro_platform.providers.cn.baostock import (
    BaoStockDailyBarsProvider,
    register_cn_baostock_provider_roles,
)
from macro_platform.providers.cn.live import CnNbsNewsProvider, CnNbsReleaseProvider
from macro_platform.providers.hk.live import HkmaPressReleaseProvider
from macro_platform.providers.hk.release_calendar import (
    HK_CENSTATD_CALENDAR_ROLE,
    HkCenstatdReleaseCalendarProvider,
)
from macro_platform.providers.hk.xtquant import (
    HkXtQuantDailyBarsProvider,
    register_hk_xtquant_provider_roles,
)
from macro_platform.providers.registry import ProviderRegistry
from macro_platform.providers.us.release_calendar import (
    US_OFFICIAL_CALENDAR_ROLE,
    UsOfficialReleaseCalendarProvider,
)
from macro_platform.providers.us.twelve_data import (
    TWELVE_DATA_DEFAULT_INSTRUMENTS,
    TwelveDataDailyBarsProvider,
    register_us_twelve_data_provider_roles,
)
from macro_platform.storage.database import Database

NOW = datetime(2026, 7, 28, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_job_029_registers_all_available_reviewed_live_tasks() -> None:
    registry = ProviderRegistry()
    register_cn_baostock_provider_roles(
        registry,
        BaoStockDailyBarsProvider(cursor_signing_secret="test-cursor-secret"),
    )
    register_hk_xtquant_provider_roles(
        registry,
        HkXtQuantDailyBarsProvider(cursor_signing_secret="test-cursor-secret"),
    )
    register_us_twelve_data_provider_roles(
        registry,
        TwelveDataDailyBarsProvider(
            api_key=SecretStr("test-api-key"),
            instruments=TWELVE_DATA_DEFAULT_INSTRUMENTS,
            cursor_signing_secret="test-cursor-secret",
        ),
    )
    cn_macro = CnNbsReleaseProvider(cursor_signing_secret="test-cursor-secret")
    registry.register(cn_macro)
    registry.bind_role(
        "cn.macro.primary", cn_macro.provider_id, required_dataset=Dataset.MACRO_RELEASES
    )
    cn_news = CnNbsNewsProvider(cursor_signing_secret="test-cursor-secret")
    registry.register(cn_news)
    registry.bind_role("cn.news.primary", cn_news.provider_id, required_dataset=Dataset.NEWS)
    hk_news = HkmaPressReleaseProvider(cursor_signing_secret="test-cursor-secret")
    registry.register(hk_news)
    registry.bind_role("hk.news.primary", hk_news.provider_id, required_dataset=Dataset.NEWS)
    hk_calendar = HkCenstatdReleaseCalendarProvider(cursor_signing_secret="test-cursor-secret")
    registry.register(hk_calendar)
    registry.bind_role(
        HK_CENSTATD_CALENDAR_ROLE,
        hk_calendar.provider_id,
        required_dataset=Dataset.MACRO_RELEASES,
    )
    us_calendar = UsOfficialReleaseCalendarProvider(cursor_signing_secret="test-cursor-secret")
    registry.register(us_calendar)
    registry.bind_role(
        US_OFFICIAL_CALENDAR_ROLE,
        us_calendar.provider_id,
        required_dataset=Dataset.MACRO_RELEASES,
    )
    database = Database("postgresql+asyncpg://macro:macro@127.0.0.1:5432/macro_data")
    try:
        tasks = build_registered_tasks(
            settings=Settings(provider_mode="live", us_provider_mode="live"),
            database=database,
            provider_registry=registry,
            now=lambda: NOW,
        )

        assert [(task.task_id, task.provider_role) for task in tasks] == [
            ("cn.daily-bars", "cn.bars.primary"),
            ("hk.core-index-bars", "hk.bars.primary"),
            ("hk.equity-bars", "hk.equity-bars.supplemental"),
            ("us.daily-bars", "us.market.primary"),
            ("cn.macro-release-calendar", "cn.macro.primary"),
            ("hk.macro-release-calendar", "hk.calendar.primary"),
            ("us.macro-release-calendar", "us.calendar.primary"),
            ("cn.official-headlines", "cn.news.primary"),
            ("hk.official-headlines", "hk.news.primary"),
        ]
        assert {task.task_id: task.required for task in tasks} == {
            "cn.daily-bars": True,
            "hk.core-index-bars": True,
            "hk.equity-bars": False,
            "us.daily-bars": True,
            "cn.macro-release-calendar": True,
            "hk.macro-release-calendar": True,
            "us.macro-release-calendar": True,
            "cn.official-headlines": True,
            "hk.official-headlines": True,
        }
    finally:
        await registry.close()
        await database.dispose()
