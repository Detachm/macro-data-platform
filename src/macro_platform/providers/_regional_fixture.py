from __future__ import annotations

import json
from base64 import b32encode
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, ClassVar, cast

from macro_platform.contracts.common import (
    AssetClass,
    AvailabilityBasis,
    Region,
    SourceRef,
    StrictModel,
    UsageRights,
)
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
    InstrumentStatus,
    Interval,
    MarketBar,
    MarketObservation,
    MarketObservationQuery,
    ScopeType,
)
from macro_platform.contracts.news import ContentMode, NewsEvent, NewsQuery, SourceTier
from macro_platform.contracts.provider import (
    Dataset,
    FetchContext,
    ProviderCapabilities,
    ProviderHealth,
    ProviderPage,
)
from macro_platform.normalization.common import canonical_json_checksum, canonicalize_url
from macro_platform.normalization.common.time import to_utc, utc_now
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

_PAGE_KEYS = frozenset({"next_cursor", "items"})
_ENVELOPE_KEYS = frozenset({"status", "fetched_at", "source_watermark", "pages"})
_ERROR_ENVELOPE_KEYS = frozenset({"status", "error"})
_ERROR_KEYS = frozenset({"code", "message", "retry_after_seconds"})
_SOURCE_KEYS = frozenset({"record_id", "source_url", "retrieved_at", "provider_updated_at"})
_INSTRUMENT_KEYS = _SOURCE_KEYS | frozenset(
    {
        "symbol",
        "mic",
        "name",
        "name_en",
        "asset_class",
        "currency",
        "timezone",
        "status",
        "listed_on",
        "delisted_on",
        "lot_size",
        "valid_from",
        "valid_to",
    }
)
_BAR_KEYS = _SOURCE_KEYS | frozenset(
    {
        "symbol",
        "mic",
        "interval",
        "bar_start",
        "bar_end",
        "trading_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover",
        "vwap",
        "currency",
        "adjustment",
        "adjustment_as_of",
        "available_at",
        "availability_basis",
        "quality_flags",
    }
)
_MARKET_OBSERVATION_KEYS = _SOURCE_KEYS | frozenset(
    {
        "scope_type",
        "scope_id",
        "metric_code",
        "value",
        "unit",
        "currency",
        "period_start",
        "period_end",
        "observed_at",
        "available_at",
        "availability_basis",
        "dimensions",
        "quality_flags",
    }
)
_MACRO_OBSERVATION_KEYS = _SOURCE_KEYS | frozenset(
    {
        "series_id",
        "period_start",
        "period_end",
        "value",
        "unit",
        "transformation",
        "released_at",
        "available_at",
        "availability_basis",
        "vintage_id",
        "revision_no",
        "value_status",
        "supersedes_observation_id",
        "quality_flags",
    }
)
_MACRO_RELEASE_KEYS = _SOURCE_KEYS | frozenset(
    {
        "series_id",
        "release_name",
        "scheduled_at",
        "released_at",
        "available_at",
        "period_start",
        "period_end",
        "actual",
        "consensus",
        "previous",
        "unit",
        "status",
    }
)
_NEWS_KEYS = _SOURCE_KEYS | frozenset(
    {
        "title",
        "summary",
        "body",
        "content_mode",
        "language",
        "source_name",
        "source_tier",
        "canonical_url",
        "published_at",
        "first_seen_at",
        "available_at",
        "availability_basis",
        "topics",
        "rights",
        "quality_flags",
    }
)
_RIGHTS_KEYS = frozenset(
    {
        "storage_allowed",
        "internal_analysis_allowed",
        "external_llm_allowed",
        "embedding_allowed",
        "redistribution_allowed",
        "content_expires_at",
    }
)


class RegionalFixtureProvider:
    """Offline parser/normalizer for synthetic regional provider fixtures."""

    provider_id: ClassVar[str]
    region: ClassVar[Region]
    source_name: ClassVar[str]
    fixture_dir: ClassVar[Path]
    macro_authority: ClassVar[str] = "STAT"
    macro_code: ClassVar[str] = "CPI_YOY"
    macro_series_name: ClassVar[str] = "CPI YoY"
    instrument_listed_on_by_symbol: ClassVar[Mapping[str, date]] = {}
    live_ready_datasets: ClassVar[frozenset[Dataset]] = frozenset()
    fixture_only_datasets: ClassVar[frozenset[Dataset]] = frozenset(
        {
            Dataset.INSTRUMENTS,
            Dataset.BARS,
            Dataset.MARKET_OBSERVATIONS,
            Dataset.MACRO_SERIES,
            Dataset.MACRO_OBSERVATIONS,
            Dataset.MACRO_RELEASES,
            Dataset.NEWS,
        }
    )

    def __init__(self, fixture_path: Path) -> None:
        self._fixture_path = fixture_path

    @classmethod
    def from_fixture(cls, fixture_name: str) -> RegionalFixtureProvider:
        return cls(cls.fixture_dir / f"{fixture_name}.json")

    def region_set(self) -> set[Region]:
        return {self.region}

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            regions={self.region},
            datasets=self.live_ready_datasets | self.fixture_only_datasets,
            intervals={Interval.D1},
            max_page_size=5000,
            supports_point_in_time=True,
            supports_revisions=True,
            supports_full_text=False,
            external_llm_allowed=False,
        )

    def assert_production_dataset_supported(self, dataset: Dataset) -> None:
        if dataset not in self.live_ready_datasets:
            raise UnsupportedCapabilityError(
                f"{self.provider_id} has no live production capability for {dataset.value}"
            )

    async def healthcheck(self) -> ProviderHealth:
        configured = self._fixture_path.exists()
        return ProviderHealth(
            provider_id=self.provider_id,
            status="ok" if configured else "not_configured",
            checked_at=utc_now(),
            latency_ms=0,
            message=None if configured else f"fixture not found: {self._fixture_path}",
        )

    async def aclose(self) -> None:
        return None

    async def fetch_instruments(
        self, query: InstrumentQuery, context: FetchContext
    ) -> ProviderPage[Instrument]:
        page = self._build_page(
            "instruments",
            self._parse_instrument,
            lambda item: (
                self.region in query.regions
                and (not query.venues or item.venue_mic in query.venues)
                and (not query.asset_classes or item.asset_class in query.asset_classes)
                and (query.active_on is None or item.valid_from <= query.active_on)
                and (
                    query.modified_since is None or item.source.retrieved_at >= query.modified_since
                )
            ),
        )
        return _limit_page(page, query.limit)

    async def fetch_bars(self, query: BarQuery, context: FetchContext) -> ProviderPage[MarketBar]:
        page = self._build_page(
            "bars",
            self._parse_bar,
            lambda item: (
                item.instrument_id in query.instrument_ids
                and item.interval == query.interval
                and item.adjustment == query.adjustment
                and query.start <= item.bar_start < query.end
                and item.available_at <= query.as_of
                and _available_for_context(item.available_at, context)
            ),
        )
        return _limit_page(page, query.limit)

    async def fetch_market_observations(
        self, query: MarketObservationQuery, context: FetchContext
    ) -> ProviderPage[MarketObservation]:
        page = self._build_page(
            "market_observations",
            self._parse_market_observation,
            lambda item: (
                item.region in query.regions
                and item.metric_code in query.metric_codes
                and (not query.scope_ids or item.scope_id in query.scope_ids)
                and query.start <= item.observed_at < query.end
                and item.available_at <= query.as_of
                and _available_for_context(item.available_at, context)
            ),
        )
        return _limit_page(page, query.limit)

    async def fetch_macro_series(
        self, query: MacroSeriesQuery, context: FetchContext
    ) -> ProviderPage[MacroSeries]:
        payload = self._payload()
        fetched_at = _datetime(_required(payload, "fetched_at", "fetched_at"), "fetched_at")
        items: list[MacroSeries] = []
        if self.region in query.regions and _available_for_context(fetched_at, context):
            items = [
                MacroSeries(
                    series_id=self._macro_series_id(),
                    region=self.region,
                    authority=self.macro_authority,
                    code=self.macro_code,
                    name=self.macro_series_name,
                    description="Synthetic CPI year-over-year series.",
                    frequency="monthly",
                    unit="percent",
                    transformation="yoy",
                    seasonal_adjustment="unknown",
                    source=SourceRef(
                        provider_id=self.provider_id,
                        provider_record_id=f"{self.region.value.lower()}-macro-series-cpi-yoy",
                        source_name=self.source_name,
                        source_url="https://example.test/macro/cpi-yoy",
                        retrieved_at=fetched_at,
                        checksum_sha256=canonical_json_checksum(
                            {
                                "provider_id": self.provider_id,
                                "series_id": self._macro_series_id(),
                            }
                        ),
                    ),
                )
            ]
        if query.series_ids:
            items = [item for item in items if item.series_id in query.series_ids]
        return ProviderPage[MacroSeries](
            items=items[: query.limit],
            next_cursor=None,
            source_watermark=_optional_str(payload, "source_watermark"),
            fetched_at=fetched_at,
            complete=True,
        )

    async def fetch_macro_observations(
        self, query: MacroObservationQuery, context: FetchContext
    ) -> ProviderPage[MacroObservation]:
        page = self._build_page(
            "macro_observations",
            self._parse_macro_observation,
            lambda item: (
                item.series_id in query.series_ids
                and query.period_from <= item.period_start <= query.period_to
                and item.available_at <= query.as_of
                and _available_for_context(item.available_at, context)
            ),
        )
        return _limit_page(page, query.limit)

    async def fetch_macro_releases(
        self, query: MacroReleaseQuery, context: FetchContext
    ) -> ProviderPage[MacroRelease]:
        page = self._build_page(
            "macro_releases",
            self._parse_macro_release,
            lambda item: (
                item.region in query.regions
                and query.scheduled_from <= item.scheduled_at < query.scheduled_to
                and item.available_at <= query.as_of
                and _available_for_context(item.available_at, context)
            ),
        )
        return _limit_page(page, query.limit)

    async def fetch_news(self, query: NewsQuery, context: FetchContext) -> ProviderPage[NewsEvent]:
        page = self._build_page(
            "news",
            self._parse_news,
            lambda item: (
                self.region in query.regions
                and query.published_from <= item.published_at < query.published_to
                and item.available_at <= query.as_of
                and (not query.languages or item.language in query.languages)
                and (not query.source_tiers or item.source_tier in query.source_tiers)
                and (not query.topics or bool(set(query.topics).intersection(item.topics)))
                and (query.include_superseded or item.supersedes_news_id is None)
                and _content_satisfies(query.content_mode, item.content_mode)
                and _available_for_context(item.available_at, context)
            ),
        )
        return _limit_page(page, query.limit)

    def _build_page[T: StrictModel](
        self,
        dataset_key: str,
        parse_item: Callable[[Mapping[str, Any]], T],
        include_item: Callable[[T], bool],
    ) -> ProviderPage[T]:
        payload = self._payload()
        _ensure_keys(payload, _ENVELOPE_KEYS, required={"status", "fetched_at", "pages"}, path="$")
        fetched_at = _datetime(_required(payload, "fetched_at", "fetched_at"), "fetched_at")
        pages = _mapping(_required(payload, "pages", "pages"), "pages")
        page_payload = _mapping(_required(pages, dataset_key, f"pages.{dataset_key}"), dataset_key)
        _ensure_keys(
            page_payload,
            _PAGE_KEYS,
            required={"next_cursor", "items"},
            path=f"pages.{dataset_key}",
        )
        raw_items = _list(_required(page_payload, "items", f"pages.{dataset_key}.items"), "items")
        parsed_items = (parse_item(_mapping(raw, dataset_key)) for raw in raw_items)
        items = [item for item in parsed_items if include_item(item)]
        next_cursor = _optional_str(page_payload, "next_cursor")
        return ProviderPage[T](
            items=items,
            next_cursor=next_cursor,
            source_watermark=_optional_str(payload, "source_watermark"),
            fetched_at=fetched_at,
            complete=next_cursor is None,
        )

    def _payload(self) -> Mapping[str, Any]:
        try:
            text = self._fixture_path.read_text(encoding="utf-8")
            if "<html" in text[:200].lower():
                raise ProviderSchemaError("provider returned a login or risk-control page")
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderSchemaError("provider payload must be JSON") from exc
        except OSError as exc:
            raise ProviderUnavailableError(f"fixture unavailable: {self._fixture_path}") from exc
        if not isinstance(payload, dict):
            raise ProviderSchemaError("provider payload must be an object")
        mapped = cast(Mapping[str, Any], payload)
        self._raise_for_status(mapped)
        return mapped

    def _raise_for_status(self, payload: Mapping[str, Any]) -> None:
        status = payload.get("status")
        if status == "ok":
            return
        if status != "error":
            raise ProviderSchemaError("provider payload status must be ok or error")
        _ensure_keys(payload, _ERROR_ENVELOPE_KEYS, required={"status", "error"}, path="$")
        error = _mapping(_required(payload, "error", "error"), "error")
        _ensure_keys(error, _ERROR_KEYS, required={"code", "message"}, path="error")
        code = _str(_required(error, "code", "error.code"), "error.code")
        message = _str(_required(error, "message", "error.message"), "error.message")
        retry_after = _optional_int(error, "retry_after_seconds")
        if code == "auth":
            raise ProviderAuthenticationError(message)
        if code == "forbidden":
            raise ProviderAuthorizationError(message)
        if code in {"429", "rate_limited"}:
            raise ProviderRateLimitError(message, retryable=True, retry_after_seconds=retry_after)
        if code == "timeout":
            raise ProviderTimeoutError(message, retryable=True)
        if code in {"schema_drift", "schema_changed"}:
            raise ProviderSchemaError(message)
        if code == "invalid_cursor":
            raise ProviderCursorError(message)
        raise ProviderUnavailableError(message)

    def _parse_instrument(self, raw: Mapping[str, Any]) -> Instrument:
        _ensure_keys(
            raw,
            _INSTRUMENT_KEYS,
            required={
                "record_id",
                "symbol",
                "mic",
                "name",
                "asset_class",
                "currency",
                "timezone",
                "status",
                "valid_from",
                "source_url",
                "retrieved_at",
            },
            path="instrument",
        )
        symbol = _str(raw["symbol"], "instrument.symbol")
        mic = _str(raw["mic"], "instrument.mic")
        listed_on = _optional_date(raw, "listed_on")
        valid_from = _date(raw["valid_from"], "instrument.valid_from")
        return Instrument(
            instrument_id=self._instrument_id(mic, symbol, listed_on or valid_from),
            canonical_symbol=f"{mic}:{symbol}",
            region=self.region,
            venue_mic=mic,
            local_symbol=symbol,
            name=_str(raw["name"], "instrument.name"),
            name_en=_optional_str(raw, "name_en"),
            asset_class=AssetClass(_str(raw["asset_class"], "instrument.asset_class")),
            currency=_str(raw["currency"], "instrument.currency"),
            timezone=_str(raw["timezone"], "instrument.timezone"),
            status=InstrumentStatus(_str(raw["status"], "instrument.status")),
            listed_on=listed_on,
            delisted_on=_optional_date(raw, "delisted_on"),
            lot_size=_optional_decimal(raw, "lot_size"),
            valid_from=valid_from,
            valid_to=_optional_date(raw, "valid_to"),
            source=self._source(raw, source_symbol=symbol),
        )

    def _parse_bar(self, raw: Mapping[str, Any]) -> MarketBar:
        _ensure_keys(
            raw,
            _BAR_KEYS,
            required={
                "record_id",
                "symbol",
                "mic",
                "interval",
                "bar_start",
                "bar_end",
                "trading_date",
                "open",
                "high",
                "low",
                "close",
                "currency",
                "adjustment",
                "available_at",
                "availability_basis",
                "quality_flags",
                "source_url",
                "retrieved_at",
            },
            path="bar",
        )
        symbol = _str(raw["symbol"], "bar.symbol")
        mic = _str(raw["mic"], "bar.mic")
        source = self._source(raw, source_symbol=symbol)
        bar_start = _datetime(raw["bar_start"], "bar.bar_start")
        bar_end = _datetime(raw["bar_end"], "bar.bar_end")
        interval = Interval(_str(raw["interval"], "bar.interval"))
        adjustment = Adjustment(_str(raw["adjustment"], "bar.adjustment"))
        instrument_id = self._instrument_id(mic, symbol)
        return MarketBar(
            bar_id=_hex_id(
                "bar",
                instrument_id,
                interval.value,
                _utc_z(bar_start),
                _utc_z(bar_end),
                adjustment.value,
                source.provider_id,
                source.provider_record_id,
            ),
            instrument_id=instrument_id,
            canonical_symbol=f"{mic}:{symbol}",
            region=self.region,
            interval=interval,
            bar_start=bar_start,
            bar_end=bar_end,
            trading_date=_date(raw["trading_date"], "bar.trading_date"),
            open=_decimal(raw["open"], "bar.open"),
            high=_decimal(raw["high"], "bar.high"),
            low=_decimal(raw["low"], "bar.low"),
            close=_decimal(raw["close"], "bar.close"),
            volume=_optional_decimal(raw, "volume"),
            turnover=_optional_decimal(raw, "turnover"),
            vwap=_optional_decimal(raw, "vwap"),
            currency=_str(raw["currency"], "bar.currency"),
            adjustment=adjustment,
            adjustment_as_of=_optional_datetime(raw, "adjustment_as_of"),
            available_at=_datetime(raw["available_at"], "bar.available_at"),
            availability_basis=AvailabilityBasis(
                _str(raw["availability_basis"], "bar.availability_basis")
            ),
            source=source,
            quality_flags=_str_list(raw["quality_flags"], "bar.quality_flags"),
        )

    def _parse_market_observation(self, raw: Mapping[str, Any]) -> MarketObservation:
        _ensure_keys(
            raw,
            _MARKET_OBSERVATION_KEYS,
            required={
                "record_id",
                "scope_type",
                "scope_id",
                "metric_code",
                "unit",
                "period_start",
                "period_end",
                "observed_at",
                "available_at",
                "availability_basis",
                "dimensions",
                "quality_flags",
                "source_url",
                "retrieved_at",
            },
            path="market_observation",
        )
        return MarketObservation(
            observation_id=_hex_id("mobs", self.provider_id, raw["record_id"]),
            region=self.region,
            scope_type=ScopeType(_str(raw["scope_type"], "market_observation.scope_type")),
            scope_id=_str(raw["scope_id"], "market_observation.scope_id"),
            metric_code=_str(raw["metric_code"], "market_observation.metric_code"),
            value=_optional_decimal(raw, "value"),
            unit=_str(raw["unit"], "market_observation.unit"),
            currency=_optional_str(raw, "currency"),
            period_start=_datetime(raw["period_start"], "market_observation.period_start"),
            period_end=_datetime(raw["period_end"], "market_observation.period_end"),
            observed_at=_datetime(raw["observed_at"], "market_observation.observed_at"),
            available_at=_datetime(raw["available_at"], "market_observation.available_at"),
            availability_basis=AvailabilityBasis(
                _str(raw["availability_basis"], "market_observation.availability_basis")
            ),
            dimensions=_str_dict(_mapping(raw["dimensions"], "market_observation.dimensions")),
            source=self._source(raw),
            quality_flags=_str_list(raw["quality_flags"], "market_observation.quality_flags"),
        )

    def _parse_macro_observation(self, raw: Mapping[str, Any]) -> MacroObservation:
        _ensure_keys(
            raw,
            _MACRO_OBSERVATION_KEYS,
            required={
                "record_id",
                "series_id",
                "period_start",
                "period_end",
                "unit",
                "transformation",
                "available_at",
                "availability_basis",
                "vintage_id",
                "revision_no",
                "value_status",
                "quality_flags",
                "source_url",
                "retrieved_at",
            },
            path="macro_observation",
        )
        return MacroObservation(
            observation_id=_hex_id(
                "obs",
                _str(raw["series_id"], "macro_observation.series_id"),
                _str(raw["period_start"], "macro_observation.period_start"),
                _str(raw["period_end"], "macro_observation.period_end"),
                _str(raw["vintage_id"], "macro_observation.vintage_id"),
                _int(raw["revision_no"], "macro_observation.revision_no"),
                _str(raw["record_id"], "macro_observation.record_id"),
            ),
            series_id=_str(raw["series_id"], "macro_observation.series_id"),
            region=self.region,
            period_start=_date(raw["period_start"], "macro_observation.period_start"),
            period_end=_date(raw["period_end"], "macro_observation.period_end"),
            value=_optional_decimal(raw, "value"),
            unit=_str(raw["unit"], "macro_observation.unit"),
            transformation=_str(raw["transformation"], "macro_observation.transformation"),
            released_at=_optional_datetime(raw, "released_at"),
            available_at=_datetime(raw["available_at"], "macro_observation.available_at"),
            availability_basis=AvailabilityBasis(
                _str(raw["availability_basis"], "macro_observation.availability_basis")
            ),
            vintage_id=_str(raw["vintage_id"], "macro_observation.vintage_id"),
            revision_no=_int(raw["revision_no"], "macro_observation.revision_no"),
            value_status=cast(Any, _str(raw["value_status"], "macro_observation.value_status")),
            supersedes_observation_id=_optional_str(raw, "supersedes_observation_id"),
            source=self._source(raw),
            quality_flags=_str_list(raw["quality_flags"], "macro_observation.quality_flags"),
        )

    def _parse_macro_release(self, raw: Mapping[str, Any]) -> MacroRelease:
        _ensure_keys(
            raw,
            _MACRO_RELEASE_KEYS,
            required={
                "record_id",
                "series_id",
                "release_name",
                "scheduled_at",
                "available_at",
                "period_start",
                "period_end",
                "unit",
                "status",
                "source_url",
                "retrieved_at",
            },
            path="macro_release",
        )
        return MacroRelease(
            release_id=_hex_id(
                "rel",
                _str(raw["series_id"], "macro_release.series_id"),
                _utc_z(_datetime(raw["scheduled_at"], "macro_release.scheduled_at")),
                _str(raw["period_start"], "macro_release.period_start"),
                _str(raw["period_end"], "macro_release.period_end"),
                _str(raw["release_name"], "macro_release.release_name"),
            ),
            series_id=_str(raw["series_id"], "macro_release.series_id"),
            region=self.region,
            release_name=_str(raw["release_name"], "macro_release.release_name"),
            scheduled_at=_datetime(raw["scheduled_at"], "macro_release.scheduled_at"),
            released_at=_optional_datetime(raw, "released_at"),
            available_at=_datetime(raw["available_at"], "macro_release.available_at"),
            period_start=_date(raw["period_start"], "macro_release.period_start"),
            period_end=_date(raw["period_end"], "macro_release.period_end"),
            actual=_optional_decimal(raw, "actual"),
            consensus=_optional_decimal(raw, "consensus"),
            previous=_optional_decimal(raw, "previous"),
            unit=_str(raw["unit"], "macro_release.unit"),
            status=cast(Any, _str(raw["status"], "macro_release.status")),
            source=self._source(raw),
        )

    def _parse_news(self, raw: Mapping[str, Any]) -> NewsEvent:
        _ensure_keys(
            raw,
            _NEWS_KEYS,
            required={
                "record_id",
                "title",
                "content_mode",
                "language",
                "source_name",
                "source_tier",
                "published_at",
                "first_seen_at",
                "available_at",
                "availability_basis",
                "topics",
                "rights",
                "quality_flags",
                "source_url",
                "retrieved_at",
            },
            path="news",
        )
        rights = self._usage_rights(_mapping(raw["rights"], "news.rights"))
        content_mode = ContentMode(_str(raw["content_mode"], "news.content_mode"))
        body = _optional_str(raw, "body")
        quality_flags = _str_list(raw["quality_flags"], "news.quality_flags")
        if body is not None and (
            content_mode is not ContentMode.FULL_TEXT or not rights.storage_allowed
        ):
            body = None
            quality_flags = [*quality_flags, "body_omitted_by_rights"]
        summary = _optional_str(raw, "summary")
        content_hash = canonical_json_checksum(
            {"title": raw["title"], "summary": summary, "body": body, "language": raw["language"]}
        )
        event_content_mode = content_mode
        if body is None and content_mode is ContentMode.FULL_TEXT:
            event_content_mode = ContentMode.SNIPPET
        return NewsEvent(
            news_id=_hex_id(
                "news",
                self.provider_id,
                _str(raw["record_id"], "news.record_id"),
                _optional_str(raw, "canonical_url") or _str(raw["title"], "news.title"),
                _utc_z(_datetime(raw["published_at"], "news.published_at")),
            ),
            cluster_id=_hex_id(
                "cluster",
                self.region.value,
                _str(raw["title"], "news.title"),
                _date_from_datetime(
                    _datetime(raw["published_at"], "news.published_at")
                ).isoformat(),
            ),
            title=_str(raw["title"], "news.title"),
            summary=summary,
            body=body,
            content_mode=event_content_mode,
            language=_str(raw["language"], "news.language"),
            source_name=_str(raw["source_name"], "news.source_name"),
            source_tier=SourceTier(_str(raw["source_tier"], "news.source_tier")),
            canonical_url=canonicalize_url(_str(raw["canonical_url"], "news.canonical_url"))
            if "canonical_url" in raw and raw["canonical_url"] is not None
            else None,
            published_at=_datetime(raw["published_at"], "news.published_at"),
            first_seen_at=_datetime(raw["first_seen_at"], "news.first_seen_at"),
            available_at=_datetime(raw["available_at"], "news.available_at"),
            availability_basis=AvailabilityBasis(
                _str(raw["availability_basis"], "news.availability_basis")
            ),
            regions=[self.region],
            topics=_str_list(raw["topics"], "news.topics"),
            content_hash_sha256=content_hash,
            usage_rights=rights,
            source=self._source(raw, source_name=_str(raw["source_name"], "news.source_name")),
            quality_flags=quality_flags,
        )

    def _source(
        self,
        raw: Mapping[str, Any],
        *,
        source_symbol: str | None = None,
        source_name: str | None = None,
    ) -> SourceRef:
        return SourceRef(
            provider_id=self.provider_id,
            provider_record_id=self._provider_record_id(raw),
            source_name=source_name or self.source_name,
            source_url=_str(raw["source_url"], "source.source_url"),
            source_symbol=source_symbol,
            retrieved_at=_datetime(raw["retrieved_at"], "source.retrieved_at"),
            provider_updated_at=_optional_datetime(raw, "provider_updated_at"),
            checksum_sha256=canonical_json_checksum(_source_checksum_payload(raw)),
        )

    def _provider_record_id(self, raw: Mapping[str, Any]) -> str:
        symbol = _optional_str(raw, "symbol")
        mic = _optional_str(raw, "mic")
        if symbol is not None and mic is not None and raw.get("trading_date") is not None:
            available_at = _optional_datetime(raw, "available_at") or _datetime(
                raw["retrieved_at"], "source.retrieved_at"
            )
            trading_date = _str(raw["trading_date"], "source.trading_date")
            interval = _str(raw["interval"], "source.interval")
            adjustment = _str(raw["adjustment"], "source.adjustment")
            available_date = _date_from_datetime(available_at)
            return (
                f"{self.provider_id}:{mic}:{symbol}:"
                f"{trading_date}:{interval}:{adjustment}:{available_date}"
            )
        if symbol is not None and mic is not None and raw.get("valid_from") is not None:
            valid_from = _str(raw["valid_from"], "source.valid_from")
            return f"{self.provider_id}:{mic}:{symbol}:{valid_from}"
        return _str(raw["record_id"], "source.record_id")

    def _usage_rights(self, raw: Mapping[str, Any]) -> UsageRights:
        _ensure_keys(
            raw,
            _RIGHTS_KEYS,
            required={
                "storage_allowed",
                "internal_analysis_allowed",
                "external_llm_allowed",
                "embedding_allowed",
                "redistribution_allowed",
            },
            path="news.rights",
        )
        return UsageRights(
            storage_allowed=_bool(raw["storage_allowed"], "news.rights.storage_allowed"),
            internal_analysis_allowed=_bool(
                raw["internal_analysis_allowed"], "news.rights.internal_analysis_allowed"
            ),
            external_llm_allowed=_bool(
                raw["external_llm_allowed"], "news.rights.external_llm_allowed"
            ),
            embedding_allowed=_bool(raw["embedding_allowed"], "news.rights.embedding_allowed"),
            redistribution_allowed=_bool(
                raw["redistribution_allowed"], "news.rights.redistribution_allowed"
            ),
            content_expires_at=_optional_datetime(raw, "content_expires_at"),
        )

    def _instrument_id(self, mic: str, symbol: str, listed_on: date | None = None) -> str:
        instrument_listed_on = listed_on or self.instrument_listed_on_by_symbol.get(
            f"{mic}:{symbol}"
        )
        if instrument_listed_on is None:
            raise ProviderSchemaError(
                f"instrument listed_on is required to derive instrument_id for {mic}:{symbol}"
            )
        seed = f"{self.region.value}|{mic}|{symbol}|{instrument_listed_on.isoformat()}"
        digest = b32encode(sha256(seed.encode("utf-8")).digest()).decode("ascii").lower()
        return f"ins_{digest[:26]}"

    def _macro_series_id(self) -> str:
        return f"macro:{self.region.value}:{self.macro_authority}:{self.macro_code}"


def _limit_page[T: StrictModel](page: ProviderPage[T], limit: int) -> ProviderPage[T]:
    items = page.items[:limit]
    return ProviderPage[T](
        items=items,
        next_cursor=page.next_cursor if len(page.items) > limit else None,
        source_watermark=page.source_watermark,
        fetched_at=page.fetched_at,
        complete=len(page.items) <= limit and page.complete,
        warnings=page.warnings,
    )


def _content_satisfies(requested: ContentMode, actual: ContentMode) -> bool:
    if requested is ContentMode.HEADLINE:
        return actual in {ContentMode.HEADLINE, ContentMode.SNIPPET, ContentMode.FULL_TEXT}
    if requested is ContentMode.SNIPPET:
        return actual in {ContentMode.SNIPPET, ContentMode.FULL_TEXT}
    return actual is ContentMode.FULL_TEXT


def _available_for_context(available_at: datetime, context: FetchContext) -> bool:
    return available_at <= context.as_of


def _hex_id(prefix: str, *parts: object) -> str:
    canonical = "|".join(str(part) for part in parts)
    return f"{prefix}_{sha256(canonical.encode('utf-8')).hexdigest()[:32]}"


def _utc_z(value: datetime) -> str:
    return to_utc(value).isoformat().replace("+00:00", "Z")


def _date_from_datetime(value: datetime) -> date:
    return to_utc(value).date()


def _source_checksum_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in raw.items()
        if key not in {"retrieved_at", "http_headers", "download_path", "batch_id"}
    }


def _ensure_keys(
    value: Mapping[str, Any], allowed: Iterable[str], *, required: Iterable[str], path: str
) -> None:
    actual = set(value)
    extra = actual - set(allowed)
    if extra:
        raise ProviderSchemaError(f"unexpected field(s) at {path}: {', '.join(sorted(extra))}")
    missing = set(required) - actual
    if missing:
        raise ProviderSchemaError(f"missing field(s) at {path}: {', '.join(sorted(missing))}")


def _required(value: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in value or value[key] is None:
        raise ProviderSchemaError(f"missing field at {path}")
    return value[key]


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ProviderSchemaError(f"{path} must be an object")
    return cast(Mapping[str, Any], value)


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProviderSchemaError(f"{path} must be a list")
    return value


def _str(value: Any, path: str) -> str:
    if not isinstance(value, str) or value == "":
        raise ProviderSchemaError(f"{path} must be a non-empty string")
    return value


def _optional_str(value: Mapping[str, Any], key: str) -> str | None:
    raw = value.get(key)
    if raw is None:
        return None
    return _str(raw, key)


def _str_list(value: Any, path: str) -> list[str]:
    raw = _list(value, path)
    if not all(isinstance(item, str) for item in raw):
        raise ProviderSchemaError(f"{path} must be a list of strings")
    return cast(list[str], raw)


def _str_dict(value: Mapping[str, Any]) -> dict[str, str]:
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise ProviderSchemaError("dimensions must be string keys and values")
    return dict(value)


def _bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ProviderSchemaError(f"{path} must be a boolean")
    return value


def _int(value: Any, path: str) -> int:
    if not isinstance(value, int):
        raise ProviderSchemaError(f"{path} must be an integer")
    return value


def _optional_int(value: Mapping[str, Any], key: str) -> int | None:
    raw = value.get(key)
    if raw is None:
        return None
    return _int(raw, key)


def _decimal(value: Any, path: str) -> Decimal:
    if not isinstance(value, str | int):
        raise ProviderSchemaError(f"{path} must be a decimal string")
    return Decimal(str(value))


def _optional_decimal(value: Mapping[str, Any], key: str) -> Decimal | None:
    raw = value.get(key)
    if raw is None:
        return None
    return _decimal(raw, key)


def _date(value: Any, path: str) -> date:
    try:
        return date.fromisoformat(_str(value, path))
    except ValueError as exc:
        raise ProviderSchemaError(f"{path} must be an ISO date") from exc


def _optional_date(value: Mapping[str, Any], key: str) -> date | None:
    raw = value.get(key)
    if raw is None:
        return None
    return _date(raw, key)


def _datetime(value: Any, path: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_str(value, path).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderSchemaError(f"{path} must be an ISO datetime") from exc
    try:
        return to_utc(parsed)
    except ValueError as exc:
        raise ProviderSchemaError(f"{path} must include a timezone") from exc


def _optional_datetime(value: Mapping[str, Any], key: str) -> datetime | None:
    raw = value.get(key)
    if raw is None:
        return None
    return _datetime(raw, key)
