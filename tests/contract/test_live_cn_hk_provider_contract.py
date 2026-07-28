from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

import httpx
import pytest

from macro_platform.contracts.common import Region
from macro_platform.contracts.macro import (
    MacroObservationQuery,
    MacroReleaseQuery,
    MacroSeriesQuery,
)
from macro_platform.contracts.news import NewsQuery
from macro_platform.contracts.provider import Dataset, FetchContext
from macro_platform.providers.cn.live import CnNbsNewsProvider, CnNbsReleaseProvider
from macro_platform.providers.hk.live import HkCsdProvider, HkmaPressReleaseProvider
from tests.contract.provider_suite import (
    assert_available_at_not_after_as_of,
    assert_capabilities_contract,
    assert_news_contract,
    assert_page_contract,
    assert_page_provenance,
)

NOW = datetime(2026, 7, 27, 4, 0, tzinfo=UTC)
CONTEXT = FetchContext(
    request_id=UUID("00000000-0000-4000-8000-000000000028"),
    as_of=NOW,
    deadline_at=datetime(2026, 7, 27, 4, 1, tzinfo=UTC),
)


@pytest.mark.asyncio
async def test_live_capabilities_are_limited_to_the_allowlisted_matrix() -> None:
    expected = {
        CnNbsReleaseProvider: ({Region.CN}, {Dataset.MACRO_RELEASES}),
        CnNbsNewsProvider: ({Region.CN}, {Dataset.NEWS}),
        HkCsdProvider: ({Region.HK}, {Dataset.MACRO_SERIES, Dataset.MACRO_OBSERVATIONS}),
        HkmaPressReleaseProvider: ({Region.HK}, {Dataset.NEWS}),
    }

    for provider_type, (regions, datasets) in expected.items():
        provider = provider_type(client=httpx.AsyncClient())
        capabilities = assert_capabilities_contract(provider)
        assert capabilities.regions == regions
        assert capabilities.datasets == datasets
        assert capabilities.supports_full_text is False
        await provider.aclose()


@pytest.mark.asyncio
async def test_cn_live_release_contract_has_provenance_and_bounded_page() -> None:
    html = """
    <h1>2026年国家统计局主要统计信息发布日程表</h1>
    <table>
      <tr><th>序号</th><th>内容</th><th>7月</th></tr>
      <tr><td>1</td><td>采购经理指数</td><td>27/一 09:30</td></tr>
    </table>
    """
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=html, request=request)
        )
    )
    provider = CnNbsReleaseProvider(client=client, clock=lambda: NOW)

    page = await provider.fetch_macro_releases(
        MacroReleaseQuery(
            regions={Region.CN},
            scheduled_from=datetime(2026, 7, 27, tzinfo=UTC),
            scheduled_to=datetime(2026, 7, 28, tzinfo=UTC),
            as_of=NOW,
            limit=1,
        ),
        CONTEXT,
    )

    assert_page_contract(page)
    assert_page_provenance(page, provider.provider_id)
    assert_available_at_not_after_as_of(page, NOW)
    assert page.complete is True
    assert page.items[0].time_precision == "instant"
    await provider.aclose()


@pytest.mark.asyncio
async def test_cn_nbs_live_news_contract_is_official_headline_metadata_only() -> None:
    html = """
    <div class="list-content">
      <ul><li>
        <a href="./202607/t20260727_1964194.html" title="CN official data release">item</a>
        <span>2026-07-27</span>
      </li></ul>
    </div>
    """
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=html, request=request)
        )
    )
    provider = CnNbsNewsProvider(client=client, clock=lambda: NOW)

    page = await provider.fetch_news(
        NewsQuery(
            regions={Region.CN},
            published_from=datetime(2026, 7, 26, tzinfo=UTC),
            published_to=datetime(2026, 7, 28, tzinfo=UTC),
            as_of=NOW,
            limit=1,
        ),
        CONTEXT,
    )

    assert_page_contract(page)
    assert_page_provenance(page, provider.provider_id)
    assert_available_at_not_after_as_of(page, NOW)
    assert_news_contract(page)
    assert page.items[0].source_tier.value == "official"
    assert page.items[0].content_mode.value == "headline"
    assert page.items[0].summary is None
    assert page.items[0].body is None
    assert page.items[0].time_precision == "date"
    await provider.aclose()


@pytest.mark.asyncio
async def test_hk_csd_live_contract_covers_series_and_observation_provenance() -> None:
    payload = {
        "header": {"status": {"name": "Success", "code": 0}},
        "dataSet": [
            {
                "freq": "M",
                "period": "202605",
                "sv": "SCC_CM",
                "svDesc": "Average monthly rate of change during the latest 3 months (%)",
                "figure": "1.5",
            }
        ],
    }
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload, request=request)
        )
    )
    provider = HkCsdProvider(client=client, clock=lambda: NOW)

    series_page = await provider.fetch_macro_series(
        MacroSeriesQuery(regions={Region.HK}, limit=1), CONTEXT
    )
    series_id = series_page.items[0].series_id
    observation_page = await provider.fetch_macro_observations(
        MacroObservationQuery(
            series_ids=[series_id],
            period_from=date(2026, 5, 1),
            period_to=date(2026, 5, 31),
            as_of=NOW,
            limit=1,
        ),
        CONTEXT,
    )

    assert_page_contract(series_page)
    assert_page_provenance(series_page, provider.provider_id)
    assert_page_contract(observation_page)
    assert_page_provenance(observation_page, provider.provider_id)
    assert_available_at_not_after_as_of(observation_page, NOW)
    assert observation_page.items[0].value_status == "preliminary"
    assert provider.capabilities().supports_revisions is False
    await provider.aclose()


@pytest.mark.asyncio
async def test_hkma_live_news_contract_is_headline_only_with_explicit_rights() -> None:
    payload = {
        "header": {"success": True, "err_code": "0000"},
        "result": {
            "datasize": 1,
            "records": [
                {
                    "title": "HKMA press release",
                    "link": "https://www.hkma.gov.hk/eng/news/1/",
                    "date": "2026-07-27",
                }
            ],
        },
    }
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload, request=request)
        )
    )
    provider = HkmaPressReleaseProvider(client=client, clock=lambda: NOW)

    page = await provider.fetch_news(
        NewsQuery(
            regions={Region.HK},
            published_from=datetime(2026, 7, 26, tzinfo=UTC),
            published_to=datetime(2026, 7, 28, tzinfo=UTC),
            as_of=NOW,
            limit=1,
        ),
        CONTEXT,
    )

    assert_page_contract(page)
    assert_page_provenance(page, provider.provider_id)
    assert_available_at_not_after_as_of(page, NOW)
    assert_news_contract(page)
    assert page.items[0].content_mode.value == "headline"
    assert page.items[0].body is None
    assert page.items[0].time_precision == "date"
    await provider.aclose()
