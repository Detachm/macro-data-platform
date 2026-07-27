from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from macro_platform.config import Settings
from macro_platform.contracts.common import Region
from macro_platform.contracts.macro import (
    MacroObservationQuery,
    MacroReleaseQuery,
    MacroSeriesQuery,
    RevisionPolicy,
)
from macro_platform.contracts.news import NewsQuery
from macro_platform.contracts.provider import FetchContext
from macro_platform.providers.base import (
    ProviderAuthenticationError,
    ProviderAuthorizationError,
    ProviderCursorError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    UnsupportedCapabilityError,
)
from macro_platform.providers.cn.live import CnNbsReleaseProvider, parse_nbs_release_calendar
from macro_platform.providers.factory import create_provider_registry
from macro_platform.providers.hk.live import HkCsdProvider, HkmaPressReleaseProvider
from macro_platform.providers.registry import ProviderRegistryError

NOW = datetime(2026, 7, 27, 4, 0, tzinfo=UTC)
CONTEXT = FetchContext(
    request_id=UUID("00000000-0000-4000-8000-000000000028"),
    as_of=NOW,
    deadline_at=datetime(2026, 7, 27, 4, 1, tzinfo=UTC),
)
LIVE_FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures"


def _recorded_fixture(name: str) -> str:
    return (LIVE_FIXTURE_ROOT / name).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_cn_nbs_release_adapter_maps_allowlisted_schedule_rows() -> None:
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
        ),
        CONTEXT,
    )

    assert page.complete is True
    assert len(page.items) == 1
    release = page.items[0]
    assert release.region is Region.CN
    assert release.release_name == "采购经理指数"
    assert release.scheduled_at == datetime(2026, 7, 27, 1, 30, tzinfo=UTC)
    assert release.actual is None
    assert release.source.provider_id == CnNbsReleaseProvider.provider_id

    await provider.aclose()


def test_cn_nbs_date_only_release_keeps_date_precision() -> None:
    releases = parse_nbs_release_calendar(
        """
        <h1>2026年国家统计局主要统计信息发布日程表</h1>
        <table>
          <tr><th>序号</th><th>内容</th><th>7月</th></tr>
          <tr><td>1</td><td>国民经济运行情况</td><td>27/一</td></tr>
        </table>
        """,
        year=2026,
        fetched_at=NOW,
        source_url="https://www.stats.gov.cn/sj/fbrc/bnxxfb/",
        provider_id=CnNbsReleaseProvider.provider_id,
        source_name=CnNbsReleaseProvider.source_name,
    )

    assert len(releases) == 1
    assert releases[0].scheduled_at is None
    assert releases[0].scheduled_date == date(2026, 7, 27)
    assert releases[0].time_precision == "date"


def test_recorded_live_fixture_manifests_are_provenanced() -> None:
    cn_manifest = json.loads(_recorded_fixture("cn/live/manifest.json"))
    hk_manifest = json.loads(_recorded_fixture("hk/live/manifest.json"))

    assert cn_manifest["fixture_kind"] == "recorded_upstream_response"
    assert hk_manifest["fixture_kind"] == "recorded_upstream_response"
    for relative_dir, manifest in (("cn/live", cn_manifest), ("hk/live", hk_manifest)):
        for name, metadata in manifest["fixtures"].items():
            path = LIVE_FIXTURE_ROOT / relative_dir / name
            assert path.exists()
            assert sha256(path.read_bytes()).hexdigest() == metadata["sha256"]


@pytest.mark.asyncio
async def test_recorded_upstream_fixtures_parse_through_live_adapters() -> None:
    nbs_provider = CnNbsReleaseProvider(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    text=_recorded_fixture("cn/live/nbs_release_calendar.html"),
                    request=request,
                )
            )
        ),
        clock=lambda: NOW,
    )
    nbs_page = await nbs_provider.fetch_macro_releases(
        MacroReleaseQuery(
            regions={Region.CN},
            scheduled_from=datetime(2026, 7, 1, tzinfo=UTC),
            scheduled_to=datetime(2026, 8, 1, tzinfo=UTC),
            as_of=NOW,
        ),
        CONTEXT,
    )
    assert {item.time_precision for item in nbs_page.items} == {"instant", "date"}
    await nbs_provider.aclose()

    csd_payload = json.loads(_recorded_fixture("hk/live/csd_510-60004.json"))
    csd_provider = HkCsdProvider(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=csd_payload, request=request)
            )
        ),
        clock=lambda: NOW,
    )
    csd_page = await csd_provider.fetch_macro_observations(
        MacroObservationQuery(
            series_ids=["macro:HK:CENSTATD:510-60004"],
            period_from=date(2026, 5, 1),
            period_to=date(2026, 6, 30),
            as_of=NOW,
        ),
        CONTEXT,
    )
    assert {item.series_id for item in csd_page.items} == {"macro:HK:CENSTATD:510-60004"}
    await csd_provider.aclose()

    hkma_payload = json.loads(_recorded_fixture("hk/live/hkma_press_releases.json"))
    hkma_provider = HkmaPressReleaseProvider(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=hkma_payload, request=request)
            )
        ),
        clock=lambda: NOW,
    )
    hkma_page = await hkma_provider.fetch_news(
        NewsQuery(
            regions={Region.HK},
            published_from=datetime(2026, 7, 1, tzinfo=UTC),
            published_to=datetime(2026, 8, 1, tzinfo=UTC),
            as_of=NOW,
        ),
        CONTEXT,
    )
    assert hkma_page.items[0].published_at is None
    assert hkma_page.items[0].published_date == date(2026, 7, 27)
    await hkma_provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [(403, ProviderAuthorizationError), (429, ProviderRateLimitError)],
)
async def test_live_adapter_classifies_http_errors(
    status: int, error_type: type[Exception]
) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, headers={"Retry-After": "4"}, request=request)
        )
    )
    provider = CnNbsReleaseProvider(client=client, clock=lambda: NOW)

    with pytest.raises(error_type):
        await provider.fetch_macro_releases(
            MacroReleaseQuery(
                regions={Region.CN},
                scheduled_from=NOW,
                scheduled_to=datetime(2026, 8, 1, tzinfo=UTC),
                as_of=NOW,
            ),
            CONTEXT,
        )

    await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [(401, ProviderAuthenticationError), (500, ProviderUnavailableError)],
)
async def test_live_adapter_classifies_authentication_and_upstream_failures(
    status: int, error_type: type[Exception]
) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, request=request))
    )
    provider = CnNbsReleaseProvider(client=client, clock=lambda: NOW)

    with pytest.raises(error_type):
        await provider.fetch_macro_releases(
            MacroReleaseQuery(
                regions={Region.CN},
                scheduled_from=NOW,
                scheduled_to=datetime(2026, 8, 1, tzinfo=UTC),
                as_of=NOW,
            ),
            CONTEXT,
        )

    await provider.aclose()


@pytest.mark.asyncio
async def test_live_healthcheck_marks_authentication_as_not_configured() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(403, request=request))
    )
    provider = CnNbsReleaseProvider(client=client, clock=lambda: NOW)

    health = await provider.healthcheck()

    assert health.status == "not_configured"
    await provider.aclose()


@pytest.mark.asyncio
async def test_live_adapter_maps_transport_timeout() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("test timeout", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
    provider = CnNbsReleaseProvider(client=client, clock=lambda: NOW)

    with pytest.raises(ProviderTimeoutError):
        await provider.fetch_macro_releases(
            MacroReleaseQuery(
                regions={Region.CN},
                scheduled_from=NOW,
                scheduled_to=datetime(2026, 8, 1, tzinfo=UTC),
                as_of=NOW,
            ),
            CONTEXT,
        )

    await provider.aclose()


@pytest.mark.asyncio
async def test_hk_csd_adapter_normalizes_series_and_observations() -> None:
    payload = {
        "header": {
            "status": {"name": "Success", "code": 0},
            "title": "Consumer Price Index",
            "count": {"finished": "2026-07-27T03:52:37+00:00"},
        },
        "dataSet": [
            {
                "freq": "M",
                "period": "202606",
                "sv": "CPI_COMP",
                "svDesc": "Composite CPI (%)",
                "figure": "1.6",
            },
            {
                "freq": "M",
                "period": "202605",
                "sv": "CPI_COMP",
                "svDesc": "Composite CPI (%)",
                "figure": "1.5",
            },
        ],
    }
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload, request=request)
        )
    )
    provider = HkCsdProvider(client=client, clock=lambda: NOW)

    series_page = await provider.fetch_macro_series(MacroSeriesQuery(regions={Region.HK}), CONTEXT)
    series_id = series_page.items[0].series_id
    observations = await provider.fetch_macro_observations(
        MacroObservationQuery(
            series_ids=[series_id],
            period_from=date(2026, 5, 1),
            period_to=date(2026, 6, 30),
            as_of=NOW,
        ),
        CONTEXT,
    )

    assert series_page.complete is True
    assert series_page.items[0].code == "510-60004"
    assert series_page.items[0].frequency.value == "monthly"
    assert series_page.items[0].transformation == "yoy"
    assert series_page.items[0].unit == "percent"
    assert [item.period_start for item in observations.items] == [
        date(2026, 5, 1),
        date(2026, 6, 1),
    ]
    assert observations.items[0].value == 1.5
    assert observations.items[0].availability_basis.value == "first_seen"

    await provider.aclose()


@pytest.mark.asyncio
async def test_hk_csd_adapter_rejects_unregistered_series() -> None:
    payload = {
        "header": {"status": {"name": "Success", "code": 0}},
        "dataSet": [
            {
                "freq": "M",
                "period": "202605",
                "sv": "NEW_UPSTREAM_SERIES",
                "svDesc": "Unknown (%)",
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

    with pytest.raises(ProviderSchemaError, match="not in the approved registry"):
        await provider.fetch_macro_series(MacroSeriesQuery(regions={Region.HK}), CONTEXT)

    await provider.aclose()


@pytest.mark.asyncio
async def test_hk_csd_adapter_rejects_changed_snapshot_cursor() -> None:
    payload = {
        "header": {"status": {"name": "Success", "code": 0}},
        "dataSet": [
            {
                "freq": "M",
                "period": "202605",
                "sv": "CPI_COMP",
                "svDesc": "Composite CPI (%)",
                "figure": "1.5",
            },
            {
                "freq": "M",
                "period": "202606",
                "sv": "CPI_COMP",
                "svDesc": "Composite CPI (%)",
                "figure": "1.6",
            },
        ],
    }
    changed_payload = {**payload, "dataSet": [*payload["dataSet"]]}
    changed_payload["dataSet"][1] = {**changed_payload["dataSet"][1], "figure": "9.9"}
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        response_payload = payload if calls == 1 else changed_payload
        return httpx.Response(200, json=response_payload, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HkCsdProvider(client=client, clock=lambda: NOW)
    series_id = "macro:HK:CENSTATD:510-60004"
    query = MacroObservationQuery(
        series_ids=[series_id],
        period_from=date(2026, 5, 1),
        period_to=date(2026, 6, 30),
        as_of=NOW,
        limit=1,
    )

    first = await provider.fetch_macro_observations(query, CONTEXT)
    assert first.next_cursor is not None
    with pytest.raises(ProviderCursorError, match="snapshot changed"):
        await provider.fetch_macro_observations(
            query.model_copy(update={"cursor": first.next_cursor}), CONTEXT
        )

    await provider.aclose()


@pytest.mark.asyncio
async def test_non_pit_csd_observation_rejects_historical_as_of() -> None:
    payload = {
        "header": {"status": {"name": "Success", "code": 0}},
        "dataSet": [
            {
                "freq": "M",
                "period": "202605",
                "sv": "CPI_COMP",
                "svDesc": "Composite CPI (%)",
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

    with pytest.raises(UnsupportedCapabilityError):
        await provider.fetch_macro_observations(
            MacroObservationQuery(
                series_ids=["macro:HK:CENSTATD:510-60004"],
                period_from=date(2026, 5, 1),
                period_to=date(2026, 5, 31),
                as_of=NOW - timedelta(days=1),
            ),
            CONTEXT,
        )

    await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revision_policy", [RevisionPolicy.FIRST_RELEASE, RevisionPolicy.ALL_VINTAGES]
)
async def test_csd_rejects_unsupported_revision_policies(
    revision_policy: RevisionPolicy,
) -> None:
    payload = {
        "header": {"status": {"name": "Success", "code": 0}},
        "dataSet": [
            {
                "freq": "M",
                "period": "202605",
                "sv": "CPI_COMP",
                "svDesc": "Composite CPI (%)",
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

    with pytest.raises(UnsupportedCapabilityError):
        await provider.fetch_macro_observations(
            MacroObservationQuery(
                series_ids=["macro:HK:CENSTATD:510-60004"],
                period_from=date(2026, 5, 1),
                period_to=date(2026, 5, 31),
                as_of=NOW,
                revision_policy=revision_policy,
            ),
            CONTEXT,
        )

    await provider.aclose()


@pytest.mark.asyncio
async def test_hkma_press_release_adapter_uses_bounded_opaque_pagination() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "offset=1" in str(request.url):
            payload = {
                "header": {"success": True, "err_code": "0000"},
                "result": {
                    "datasize": 2,
                    "records": [
                        {
                            "title": "Second release",
                            "link": "https://www.hkma.gov.hk/eng/news/2/",
                            "date": "2026-07-26",
                        }
                    ],
                },
            }
        else:
            payload = {
                "header": {"success": True, "err_code": "0000"},
                "result": {
                    "datasize": 2,
                    "records": [
                        {
                            "title": "First release",
                            "link": "https://www.hkma.gov.hk/eng/news/1/",
                            "date": "2026-07-27",
                        },
                        {
                            "title": "Second release",
                            "link": "https://www.hkma.gov.hk/eng/news/2/",
                            "date": "2026-07-26",
                        },
                    ],
                },
            }
        return httpx.Response(200, json=payload, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HkmaPressReleaseProvider(client=client, clock=lambda: NOW)
    query = NewsQuery(
        regions={Region.HK},
        published_from=datetime(2026, 7, 25, tzinfo=UTC),
        published_to=datetime(2026, 7, 28, tzinfo=UTC),
        as_of=NOW,
        limit=1,
    )

    first = await provider.fetch_news(query, CONTEXT)

    assert len(first.items) == 1
    assert first.next_cursor is not None
    assert "offset=0" in calls[0]
    assert first.items[0].body is None
    assert first.items[0].usage_rights.external_llm_allowed is True

    second = await provider.fetch_news(
        query.model_copy(update={"cursor": first.next_cursor}), CONTEXT
    )
    assert [item.title for item in second.items] == ["Second release"]
    # The adapter replays the bounded source snapshot before slicing the
    # requested logical page, so continuation does not depend on a mutable
    # upstream offset.
    assert "offset=0" in calls[1]

    await provider.aclose()


@pytest.mark.asyncio
async def test_hkma_press_release_adapter_quarantines_duplicate_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "offset=2" in str(request.url):
            records = [
                {
                    "title": "Repeated release",
                    "link": "https://www.hkma.gov.hk/eng/news/repeated/",
                    "date": "2026-07-27",
                },
                {
                    "title": "Third release",
                    "link": "https://www.hkma.gov.hk/eng/news/third/",
                    "date": "2026-07-25",
                },
            ]
        else:
            records = [
                {
                    "title": "Repeated release",
                    "link": "https://www.hkma.gov.hk/eng/news/repeated/",
                    "date": "2026-07-27",
                },
                {
                    "title": "Another release",
                    "link": "https://www.hkma.gov.hk/eng/news/another/",
                    "date": "2026-07-26",
                },
            ]
        payload = {
            "header": {"success": True, "err_code": "0000"},
            "result": {"datasize": 3, "records": records},
        }
        return httpx.Response(200, json=payload, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HkmaPressReleaseProvider(client=client, clock=lambda: NOW)
    query = NewsQuery(
        regions={Region.HK},
        published_from=datetime(2026, 7, 25, tzinfo=UTC),
        published_to=datetime(2026, 7, 28, tzinfo=UTC),
        as_of=NOW,
        limit=1,
    )

    with pytest.raises(ProviderCursorError, match="duplicate page"):
        await provider.fetch_news(query, CONTEXT)

    await provider.aclose()


@pytest.mark.asyncio
async def test_live_adapter_rejects_malformed_json_as_schema_drift() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="not-json", request=request)
        )
    )
    provider = HkCsdProvider(client=client, clock=lambda: NOW)

    with pytest.raises(ProviderSchemaError):
        await provider.fetch_macro_series(MacroSeriesQuery(regions={Region.HK}), CONTEXT)

    await provider.aclose()


@pytest.mark.asyncio
async def test_live_adapter_rejects_html_login_as_authorization_failure() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text="<!doctype html><html><body>login</body></html>",
                headers={"Content-Type": "text/html"},
                request=request,
            )
        )
    )
    provider = HkCsdProvider(client=client, clock=lambda: NOW)

    with pytest.raises(ProviderAuthorizationError):
        await provider.fetch_macro_series(MacroSeriesQuery(regions={Region.HK}), CONTEXT)

    await provider.aclose()


def test_production_requires_explicit_live_provider_mode() -> None:
    with pytest.raises(ValueError, match="PROVIDER_MODE=live"):
        Settings(app_env="production", service_token=SecretStr("runtime-token"))

    with pytest.raises(ValueError, match="PROVIDER_CURSOR_SECRET"):
        Settings(
            app_env="production",
            provider_mode="live",
            service_token=SecretStr("runtime-token"),
        )


@pytest.mark.asyncio
async def test_provider_factory_keeps_fixture_and_live_roles_separate() -> None:
    fixture_registry = create_provider_registry(
        Settings(app_env="test", provider_mode="fixture", service_token=SecretStr("token"))
    )
    assert (
        fixture_registry.resolve("cn.contract_fixture.macro_releases")
        .capabilities()
        .provider_id.startswith("cn.contract-fixture")
    )
    with pytest.raises(ProviderRegistryError):
        fixture_registry.resolve("cn.macro.primary")
    with pytest.raises(ProviderRegistryError, match="fixture provider role"):
        fixture_registry.assert_production_safe()
    await fixture_registry.close()

    live_registry = create_provider_registry(
        Settings(app_env="test", provider_mode="live", service_token=SecretStr("token"))
    )
    assert live_registry.resolve("cn.macro.primary").capabilities().datasets
    assert live_registry.resolve("hk.news.primary").capabilities().datasets
    await live_registry.close()
