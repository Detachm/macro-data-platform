from __future__ import annotations

from datetime import UTC, date, datetime
from io import BytesIO
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

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
from macro_platform.providers.hk.release_calendar import HkCenstatdReleaseCalendarProvider
from macro_platform.providers.us.release_calendar import UsOfficialReleaseCalendarProvider
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
        HkCenstatdReleaseCalendarProvider: ({Region.HK}, {Dataset.MACRO_RELEASES}),
        UsOfficialReleaseCalendarProvider: ({Region.US}, {Dataset.MACRO_RELEASES}),
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
async def test_hk_censtatd_release_calendar_contract_uses_official_release_time() -> None:
    content = _xlsx_bytes(
        [
            (
                "46230",
                "Prices",
                "Consumer Prices",
                "Consumer Price Index",
                "Consumer Price Index for July 2026",
                "",
                "",
            )
        ]
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=content, request=request)
        )
    )
    provider = HkCenstatdReleaseCalendarProvider(client=client, clock=lambda: NOW)

    page = await provider.fetch_macro_releases(
        MacroReleaseQuery(
            regions={Region.HK},
            scheduled_from=datetime(2026, 7, 26, tzinfo=UTC),
            scheduled_to=datetime(2026, 7, 28, tzinfo=UTC),
            as_of=NOW,
            limit=1,
        ),
        CONTEXT,
    )

    assert_page_contract(page)
    assert_page_provenance(page, provider.provider_id)
    assert_available_at_not_after_as_of(page, NOW)
    assert page.items[0].scheduled_at == datetime(2026, 7, 27, 8, 30, tzinfo=UTC)
    await provider.aclose()


@pytest.mark.asyncio
async def test_us_official_calendar_contract_preserves_omb_date_precision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Page:
        def extract_text(self) -> str:
            return """
            The Employment Situation
            (Data are for previous month)
            27 27 27 27 27 27 27 27 27 27 27 27
            Producer Price Indexes
            (Data are for previous month)
            27 27 27 27 27 27 27 27 27 27 27 27
            Consumer Price Index
            (Data are for previous month)
            27 27 27 27 27 27 27 27 27 27 27 27
            """

    class _Reader:
        def __init__(self, _: BytesIO, *, strict: bool) -> None:
            assert strict is True
            self.pages = [_Page()]

    monkeypatch.setattr("macro_platform.providers.us.release_calendar.PdfReader", _Reader)

    def response(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("principal-federal-economic-indicators/"):
            return httpx.Response(
                200,
                text=(
                    '<a href="/wp-content/uploads/2025/09/'
                    'pfei_schedule_release_dates_cy2026.pdf">2026</a>'
                ),
                request=request,
            )
        if request.url.path.endswith(".pdf"):
            return httpx.Response(200, content=b"fake-pdf", request=request)
        return httpx.Response(
            200,
            text="""
            <h2>Year 2026</h2><table><tr>
              <td class="scheduled-date"><div>July 27</div><small>8:30 AM</small></td>
              <td class="release-title">GDP, 2nd Quarter 2026</td>
            </tr></table>
            """,
            request=request,
        )

    provider = UsOfficialReleaseCalendarProvider(
        client=httpx.AsyncClient(transport=httpx.MockTransport(response)), clock=lambda: NOW
    )
    page = await provider.fetch_macro_releases(
        MacroReleaseQuery(
            regions={Region.US},
            scheduled_from=datetime(2026, 7, 27, tzinfo=UTC),
            scheduled_to=datetime(2026, 7, 28, tzinfo=UTC),
            as_of=NOW,
            limit=10,
        ),
        CONTEXT,
    )

    assert_page_contract(page)
    assert_page_provenance(page, provider.provider_id)
    assert_available_at_not_after_as_of(page, NOW)
    assert {item.time_precision for item in page.items} == {"date", "instant"}
    date_only = [item for item in page.items if item.time_precision == "date"]
    assert len(date_only) == 3
    assert {item.period_start for item in date_only} == {date(2026, 6, 1)}
    await provider.aclose()


@pytest.mark.asyncio
async def test_us_official_calendar_discovers_every_bea_year_in_a_cross_year_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Page:
        def extract_text(self) -> str:
            return "\n".join(
                f"{indicator}\n(Data are for previous month)\n27 27 27 27 27 27 27 27 27 27 27 27"
                for indicator in (
                    "The Employment Situation",
                    "Producer Price Indexes",
                    "Consumer Price Index",
                )
            )

    class _Reader:
        def __init__(self, _: BytesIO, *, strict: bool) -> None:
            assert strict is True
            self.pages = [_Page()]

    monkeypatch.setattr("macro_platform.providers.us.release_calendar.PdfReader", _Reader)

    def response(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("principal-federal-economic-indicators/"):
            return httpx.Response(
                200,
                text="""
                <a href="/pfei_schedule_release_dates_cy2026.pdf">2026</a>
                <a href="/pfei_schedule_release_dates_cy2027.pdf">2027</a>
                """,
                request=request,
            )
        if path.endswith(".pdf"):
            return httpx.Response(200, content=b"fake-pdf", request=request)
        if path == "/news/schedule/2027":
            return httpx.Response(
                200,
                text=_bea_contract_html(2027, "January 1", "GDP, 4th Quarter 2026"),
                request=request,
            )
        return httpx.Response(
            200,
            text=(
                '<a href="/news/schedule/2027">2027 Schedule</a>'
                + _bea_contract_html(2026, "December 31", "GDP, 3rd Quarter 2026")
            ),
            request=request,
        )

    cross_year_now = datetime(2026, 12, 31, tzinfo=UTC)
    context = CONTEXT.model_copy(
        update={
            "as_of": cross_year_now,
            "deadline_at": cross_year_now.replace(minute=1),
        }
    )
    provider = UsOfficialReleaseCalendarProvider(
        client=httpx.AsyncClient(transport=httpx.MockTransport(response)),
        clock=lambda: cross_year_now,
    )
    page = await provider.fetch_macro_releases(
        MacroReleaseQuery(
            regions={Region.US},
            scheduled_from=cross_year_now,
            scheduled_to=datetime(2027, 1, 2, tzinfo=UTC),
            as_of=cross_year_now,
            limit=10,
        ),
        context,
    )

    assert {item.release_name for item in page.items} == {
        "GDP, 3rd Quarter 2026",
        "GDP, 4th Quarter 2026",
    }
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


def _bea_contract_html(year: int, schedule: str, title: str) -> str:
    return f"""
    <h2>Year {year}</h2><table><tr>
      <td class="scheduled-date"><div>{schedule}</div><small>8:30 AM</small></td>
      <td class="release-title">{title}</td>
    </tr></table>
    """


def _xlsx_bytes(rows: list[tuple[str, ...]]) -> bytes:
    header = (
        "Release Date",
        "Subject",
        "Sub-subject",
        "Series",
        "Title",
        "Footnote No",
        "Footnote Content",
    )
    strings = [value for row in (header, *rows) for value in row if not value.isdigit()]
    string_index = {value: index for index, value in enumerate(strings)}
    xml_rows: list[str] = []
    for row_number, row in enumerate((header, *rows), start=1):
        cells: list[str] = []
        for column, value in zip("ABCDEFG", row, strict=True):
            if value.isdigit():
                cells.append(f'<c r="{column}{row_number}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{column}{row_number}" t="s"><v>{string_index[value]}</v></c>')
        xml_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    shared = "".join(f"<si><t>{value}</t></si>" for value in strings)
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"{shared}</sst>",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(xml_rows)}</sheetData></worksheet>",
        )
    return output.getvalue()
