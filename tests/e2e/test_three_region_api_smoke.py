from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient
from pydantic import SecretStr

from macro_platform.api.app import create_app
from macro_platform.config import Settings
from macro_platform.contracts.common import Region
from macro_platform.contracts.macro import (
    MacroObservation,
    MacroObservationQuery,
    MacroRelease,
    MacroReleaseQuery,
    MacroSeries,
    MacroSeriesQuery,
)
from macro_platform.contracts.market import (
    Adjustment,
    BarQuery,
    Instrument,
    InstrumentQuery,
    Interval,
    MarketBar,
    MarketObservation,
    MarketObservationQuery,
    MarketSnapshot,
    MarketSnapshotQuery,
)
from macro_platform.contracts.news import ContentMode, NewsEvent, NewsQuery
from macro_platform.contracts.provider import FetchContext
from macro_platform.providers._regional_fixture import RegionalFixtureProvider
from macro_platform.providers.cn import (
    CN_PROVIDER_ID,
    CnSyntheticProvider,
    register_cn_provider_roles,
)
from macro_platform.providers.hk import (
    HK_PROVIDER_ID,
    HkSyntheticProvider,
    register_hk_provider_roles,
)
from macro_platform.providers.registry import ProviderRegistry
from macro_platform.providers.us import (
    US_PROVIDER_ID,
    UsFixtureProvider,
    register_us_provider_roles,
)

TOKEN = "three-region-smoke-token"
AS_OF = datetime(2026, 7, 23, 8, tzinfo=UTC)
START = datetime(2026, 6, 1, tzinfo=UTC)
END = AS_OF + timedelta(microseconds=1)
REGIONS = (Region.CN, Region.HK, Region.US)
CONTEXT = FetchContext(
    request_id=UUID("00000000-0000-4000-8000-000000000009"),
    as_of=AS_OF,
    deadline_at=AS_OF + timedelta(minutes=1),
)


@dataclass
class _FixtureRepository:
    instruments: list[Instrument] = field(default_factory=list)
    bars: list[MarketBar] = field(default_factory=list)
    market_observations: list[MarketObservation] = field(default_factory=list)
    macro_series: list[MacroSeries] = field(default_factory=list)
    macro_observations: list[MacroObservation] = field(default_factory=list)
    macro_releases: list[MacroRelease] = field(default_factory=list)
    news: list[NewsEvent] = field(default_factory=list)

    async def list_instruments(self, query: InstrumentQuery) -> list[Instrument]:
        items = [
            item
            for item in self.instruments
            if item.region in query.regions
            and (not query.venues or item.venue_mic in query.venues)
            and (not query.asset_classes or item.asset_class in query.asset_classes)
            and (
                query.active_on is None
                or (
                    item.valid_from <= query.active_on
                    and (item.valid_to is None or query.active_on < item.valid_to)
                )
            )
            and (query.modified_since is None or item.source.retrieved_at >= query.modified_since)
        ]
        return sorted(items, key=lambda item: item.instrument_id)[: query.limit]

    async def list_bars(self, query: BarQuery) -> list[MarketBar]:
        items = [
            item
            for item in self.bars
            if item.instrument_id in query.instrument_ids
            and item.interval is query.interval
            and item.adjustment is query.adjustment
            and query.start <= item.bar_start < query.end
            and item.available_at <= query.as_of
        ]
        return sorted(items, key=lambda item: (item.bar_start, item.bar_id))[: query.limit]

    async def list_snapshots(self, query: MarketSnapshotQuery) -> list[MarketSnapshot]:
        return []

    async def list_market_observations(
        self, query: MarketObservationQuery
    ) -> list[MarketObservation]:
        items = [
            item
            for item in self.market_observations
            if item.region in query.regions
            and item.metric_code in query.metric_codes
            and (not query.scope_ids or item.scope_id in query.scope_ids)
            and query.start <= item.observed_at < query.end
            and item.available_at <= query.as_of
        ]
        return sorted(items, key=lambda item: (item.observed_at, item.observation_id))[
            : query.limit
        ]

    async def list_macro_series(self, query: MacroSeriesQuery) -> list[MacroSeries]:
        items = [
            item
            for item in self.macro_series
            if item.region in query.regions
            and (not query.series_ids or item.series_id in query.series_ids)
        ]
        return sorted(items, key=lambda item: item.series_id)[: query.limit]

    async def list_macro_observations(self, query: MacroObservationQuery) -> list[MacroObservation]:
        items = [
            item
            for item in self.macro_observations
            if item.series_id in query.series_ids
            and query.period_from <= item.period_start <= query.period_to
            and item.available_at <= query.as_of
        ]
        return sorted(items, key=lambda item: (item.period_end, item.observation_id))[: query.limit]

    async def list_macro_releases(self, query: MacroReleaseQuery) -> list[MacroRelease]:
        items = [
            item
            for item in self.macro_releases
            if item.region in query.regions
            and query.scheduled_from <= item.scheduled_at < query.scheduled_to
            and item.available_at <= query.as_of
        ]
        return sorted(items, key=lambda item: (item.scheduled_at, item.release_id))[: query.limit]

    async def list_news(self, query: NewsQuery) -> list[NewsEvent]:
        items = [
            item
            for item in self.news
            if set(item.regions).intersection(query.regions)
            and query.published_from <= item.published_at < query.published_to
            and item.available_at <= query.as_of
            and (
                not query.entity_ids or any(e.entity_id in query.entity_ids for e in item.entities)
            )
            and (not query.topics or set(item.topics).intersection(query.topics))
            and (not query.languages or item.language in query.languages)
            and (not query.source_tiers or item.source_tier in query.source_tiers)
            and (query.include_superseded or item.supersedes_news_id is None)
            and _content_satisfies(query.content_mode, item.content_mode)
        ]
        return sorted(items, key=lambda item: (item.published_at, item.news_id), reverse=True)[
            : query.limit
        ]


def _content_satisfies(requested: ContentMode, actual: ContentMode) -> bool:
    if requested is ContentMode.HEADLINE:
        return True
    if requested is ContentMode.SNIPPET:
        return actual in {ContentMode.SNIPPET, ContentMode.FULL_TEXT}
    return actual is ContentMode.FULL_TEXT


async def _fixture_stack() -> tuple[_FixtureRepository, ProviderRegistry]:
    cn = CnSyntheticProvider.from_fixture("success")
    hk = HkSyntheticProvider.from_fixture("success")
    us = UsFixtureProvider.from_fixture("success", clock=lambda: AS_OF)
    registry = ProviderRegistry()
    register_cn_provider_roles(registry, cn)
    register_hk_provider_roles(registry, hk)
    register_us_provider_roles(registry, us)

    repository = _FixtureRepository()
    providers: list[RegionalFixtureProvider | UsFixtureProvider] = [cn, hk, us]
    for provider in providers:
        region = next(iter(provider.capabilities().regions))
        instruments = (
            await provider.fetch_instruments(InstrumentQuery(regions={region}), CONTEXT)
        ).items
        repository.instruments.extend(instruments)
        repository.bars.extend(
            (
                await provider.fetch_bars(
                    BarQuery(
                        instrument_ids=[item.instrument_id for item in instruments],
                        interval=Interval.D1,
                        start=START,
                        end=END,
                        adjustment=Adjustment.RAW,
                        as_of=AS_OF,
                    ),
                    CONTEXT,
                )
            ).items
        )
        repository.market_observations.extend(
            (
                await provider.fetch_market_observations(
                    MarketObservationQuery(
                        regions={region},
                        metric_codes=["market.turnover", "rate.fed_funds.effective"],
                        start=START,
                        end=END,
                        as_of=AS_OF,
                    ),
                    CONTEXT,
                )
            ).items
        )
        series = (
            await provider.fetch_macro_series(MacroSeriesQuery(regions={region}), CONTEXT)
        ).items
        repository.macro_series.extend(series)
        repository.macro_observations.extend(
            (
                await provider.fetch_macro_observations(
                    MacroObservationQuery(
                        series_ids=[item.series_id for item in series],
                        period_from=date(2025, 1, 1),
                        period_to=AS_OF.date(),
                        as_of=AS_OF,
                    ),
                    CONTEXT,
                )
            ).items
        )
        repository.macro_releases.extend(
            (
                await provider.fetch_macro_releases(
                    MacroReleaseQuery(
                        regions={region},
                        scheduled_from=START,
                        scheduled_to=END + timedelta(days=7),
                        as_of=AS_OF,
                    ),
                    CONTEXT,
                )
            ).items
        )
        repository.news.extend(
            (
                await provider.fetch_news(
                    NewsQuery(
                        regions={region},
                        published_from=START,
                        published_to=END,
                        as_of=AS_OF,
                        content_mode=ContentMode.SNIPPET,
                    ),
                    CONTEXT,
                )
            ).items
        )
    return repository, registry


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _available_at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_three_region_fixture_data_flows_through_common_api_and_editor_context() -> None:
    repository, registry = asyncio.run(_fixture_stack())
    settings = Settings(app_env="test", service_token=SecretStr(TOKEN))
    app = create_app(settings=settings, repository=repository, provider_registry=registry)
    instrument_ids = [item.instrument_id for item in repository.instruments]
    series_ids = [item.series_id for item in repository.macro_series]

    with TestClient(app) as api:
        capabilities = api.get("/v1/meta/capabilities", headers=_auth())
        instruments = api.get(
            "/v1/instruments",
            headers=_auth(),
            params=[("regions", region.value) for region in REGIONS],
        )
        observations = api.get(
            "/v1/market/observations",
            headers=_auth(),
            params=[
                *(("regions", region.value) for region in REGIONS),
                ("metric_code", "market.turnover"),
                ("metric_code", "rate.fed_funds.effective"),
                ("start", START.isoformat()),
                ("end", END.isoformat()),
                ("as_of", AS_OF.isoformat()),
            ],
        )
        stored_news = api.get(
            "/v1/news",
            headers=_auth(),
            params=[
                *(("regions", region.value) for region in REGIONS),
                ("published_from", START.isoformat()),
                ("published_to", END.isoformat()),
                ("as_of", AS_OF.isoformat()),
                ("content_mode", "snippet"),
            ],
        )
        context_request = {
            "regions": [region.value for region in REGIONS],
            "as_of": AS_OF.isoformat(),
            "market": {
                "instrument_ids": instrument_ids,
                "metric_codes": ["market.turnover", "rate.fed_funds.effective"],
                "lookback_sessions": 5,
            },
            "macro": {
                "series_ids": series_ids,
                "lookback_days": 120,
                "upcoming_days": 7,
            },
            "news": {"lookback_hours": 744, "content_mode": "snippet"},
            "fail_on_incomplete": True,
        }
        first_context = api.post("/v1/editor/context", headers=_auth(), json=context_request)
        second_context = api.post("/v1/editor/context", headers=_auth(), json=context_request)

    assert capabilities.status_code == 200
    assert {item["provider_id"] for item in capabilities.json()["data"]["items"]} == {
        CN_PROVIDER_ID,
        HK_PROVIDER_ID,
        US_PROVIDER_ID,
    }
    assert instruments.status_code == 200
    assert {item["region"] for item in instruments.json()["data"]["items"]} == {
        region.value for region in REGIONS
    }
    assert observations.status_code == 200
    assert {item["region"] for item in observations.json()["data"]["items"]} == {
        region.value for region in REGIONS
    }
    assert stored_news.status_code == 200
    stored_restricted = [
        item
        for item in stored_news.json()["data"]["items"]
        if item["source"]["provider_id"] in {CN_PROVIDER_ID, HK_PROVIDER_ID}
    ]
    assert stored_restricted
    assert all(item["summary"] is not None for item in stored_restricted)

    assert first_context.status_code == 200
    assert second_context.status_code == 200
    context = first_context.json()["data"]
    assert (
        context["data_fingerprint_sha256"]
        == second_context.json()["data"]["data_fingerprint_sha256"]
    )
    assert context["context_id"] == second_context.json()["data"]["context_id"]
    assert len(context["coverage"]) == 9
    assert all(
        item["status"] == "complete" and item["record_count"] > 0 for item in context["coverage"]
    )
    assert {item["region"] for item in context["coverage"]} == {region.value for region in REGIONS}

    record_groups = [
        context["market_bars"],
        context["market_observations"],
        context["macro_observations"],
        context["macro_releases"],
        context["news_events"],
    ]
    assert all(
        _available_at(record["available_at"]) <= AS_OF
        for records in record_groups
        for record in records
    )
    provider_ids = {
        record["source"]["provider_id"] for records in record_groups for record in records
    }
    assert provider_ids == {CN_PROVIDER_ID, HK_PROVIDER_ID, US_PROVIDER_ID}
    legacy_rights_context_news = [
        item
        for item in context["news_events"]
        if item["source"]["provider_id"] in {CN_PROVIDER_ID, HK_PROVIDER_ID}
    ]
    assert legacy_rights_context_news
    assert all(
        item["summary"] is not None and item["body"] is None and item["content_mode"] == "snippet"
        for item in legacy_rights_context_news
    )

    openapi = app.openapi()
    assert not any(path.startswith(("/v1/cn", "/v1/hk", "/v1/us")) for path in openapi["paths"])
    assert not any(name.startswith(("Cn", "Hk", "US")) for name in openapi["components"]["schemas"])
