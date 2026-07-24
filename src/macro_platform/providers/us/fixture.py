"""Offline US provider vertical slice backed only by synthetic fixtures."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from macro_platform.contracts.common import (
    AssetClass,
    AvailabilityBasis,
    Region,
    SourceRef,
    StrictModel,
    UsageRights,
    WarningItem,
)
from macro_platform.contracts.macro import (
    Frequency,
    MacroObservation,
    MacroObservationQuery,
    MacroRelease,
    MacroReleaseQuery,
    MacroSeries,
    MacroSeriesQuery,
    RevisionPolicy,
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
from macro_platform.contracts.news import ContentMode, EntityRef, NewsEvent, NewsQuery, SourceTier
from macro_platform.contracts.provider import (
    Dataset,
    FetchContext,
    ProviderCapabilities,
    ProviderHealth,
    ProviderPage,
)
from macro_platform.normalization.common import canonical_json_checksum, canonicalize_url
from macro_platform.normalization.common.time import TimezoneRequiredError, utc_now
from macro_platform.normalization.us import (
    UsInstrumentIdentity,
    UsNormalizationError,
    normalize_us_alias,
    normalize_us_value,
    to_us_market_utc,
    us_trading_date,
)
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
from macro_platform.providers.registry import ProviderRegistry

US_PROVIDER_ID = "us.fixture.vertical-slice.v1"
US_ROLE_BINDINGS: dict[str, str] = {
    "us.instruments.primary": US_PROVIDER_ID,
    "us.market.primary": US_PROVIDER_ID,
    "us.rates_fx.primary": US_PROVIDER_ID,
    "us.macro.primary": US_PROVIDER_ID,
    "us.filings.primary": US_PROVIDER_ID,
    "us.news.primary": US_PROVIDER_ID,
}

_FIXTURE_DIR = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "us" / "provider"
_PAGE_KEYS = frozenset({"next_cursor", "items"})
_PAGINATED_PAGE_KEYS = _PAGE_KEYS | {"cursor"}
_DATASET_PAGE_KEYS = frozenset(dataset.value for dataset in Dataset)
_ENVELOPE_KEYS = frozenset({"status", "fetched_at", "source_watermark", "pages"})
_ERROR_ENVELOPE_KEYS = frozenset({"status", "error"})
_ERROR_KEYS = frozenset({"code", "message", "retry_after_seconds"})
_SOURCE_KEYS = frozenset(
    {"record_id", "source_name", "source_url", "retrieved_at", "provider_updated_at"}
)
_INSTRUMENT_KEYS = _SOURCE_KEYS | frozenset(
    {
        "symbol",
        "exchange",
        "issuer_key",
        "first_canonical_symbol",
        "first_valid_from",
        "name",
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
        "exchange",
        "issuer_key",
        "first_canonical_symbol",
        "first_valid_from",
        "valid_from",
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
        "currency",
        "adjustment",
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
_MACRO_SERIES_KEYS = _SOURCE_KEYS | frozenset(
    {
        "series_id",
        "authority",
        "code",
        "name",
        "description",
        "frequency",
        "unit",
        "transformation",
        "seasonal_adjustment",
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
        "accession_number",
        "title",
        "summary",
        "language",
        "source_tier",
        "canonical_url",
        "published_at",
        "first_seen_at",
        "available_at",
        "availability_basis",
        "entities",
        "topics",
        "rights",
        "quality_flags",
    }
)
_ENTITY_KEYS = frozenset({"entity_type", "entity_id", "mention", "confidence"})
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


class UsFixtureProvider:
    """Fixture-only US adapter with no network, database, or FastAPI dependency."""

    fixture_dir = _FIXTURE_DIR

    def __init__(self, fixture_path: Path, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._fixture_path = fixture_path
        self._clock = clock

    @classmethod
    def from_fixture(
        cls,
        fixture_name: str,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> UsFixtureProvider:
        return cls(cls.fixture_dir / f"{fixture_name}.json", clock=clock)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=US_PROVIDER_ID,
            regions={Region.US},
            datasets=set(Dataset),
            intervals={Interval.D1},
            max_page_size=5000,
            supports_point_in_time=True,
            supports_revisions=True,
            supports_full_text=False,
            external_llm_allowed=False,
        )

    def assert_production_dataset_supported(self, dataset: Dataset) -> None:
        raise UnsupportedCapabilityError(
            f"{US_PROVIDER_ID} is fixture-only and cannot schedule {dataset.value}"
        )

    async def healthcheck(self) -> ProviderHealth:
        configured = self._fixture_path.exists()
        return ProviderHealth(
            provider_id=US_PROVIDER_ID,
            status="ok" if configured else "not_configured",
            checked_at=self._clock(),
            latency_ms=0,
            message=None if configured else f"fixture not found: {self._fixture_path}",
        )

    async def aclose(self) -> None:
        return None

    async def fetch_instruments(
        self, query: InstrumentQuery, context: FetchContext
    ) -> ProviderPage[Instrument]:
        page = self._page_for_query(Dataset.INSTRUMENTS, self._parse_instrument, query, context)
        items = [
            item
            for item in page.items
            if Region.US in query.regions
            and (not query.venues or item.venue_mic in query.venues)
            and (not query.asset_classes or item.asset_class in query.asset_classes)
            and (query.active_on is None or _is_active_on(item, query.active_on))
            and (query.modified_since is None or item.source.retrieved_at >= query.modified_since)
            and item.source.retrieved_at <= context.as_of
        ]
        return _limited_page(
            page,
            sorted(items, key=lambda item: (item.canonical_symbol, item.valid_from)),
            query.limit,
        )

    async def fetch_bars(self, query: BarQuery, context: FetchContext) -> ProviderPage[MarketBar]:
        if query.interval is not Interval.D1 or query.adjustment is not Adjustment.RAW:
            raise UnsupportedCapabilityError(
                "the US fixture vertical slice supports raw daily bars only"
            )
        page = self._page_for_query(Dataset.BARS, self._parse_bar, query, context)
        items = [
            item
            for item in page.items
            if item.instrument_id in query.instrument_ids
            and item.interval is query.interval
            and item.adjustment is query.adjustment
            and query.start <= item.bar_start < query.end
            and _is_available(item.available_at, query.as_of, context)
        ]
        return _limited_page(
            page,
            sorted(items, key=lambda item: (item.bar_end, item.instrument_id, item.bar_id)),
            query.limit,
        )

    async def fetch_market_observations(
        self, query: MarketObservationQuery, context: FetchContext
    ) -> ProviderPage[MarketObservation]:
        page = self._page_for_query(
            Dataset.MARKET_OBSERVATIONS,
            self._parse_market_observation,
            query,
            context,
        )
        items = [
            item
            for item in page.items
            if Region.US in query.regions
            and item.metric_code in query.metric_codes
            and (not query.scope_ids or item.scope_id in query.scope_ids)
            and query.start <= item.observed_at < query.end
            and _is_available(item.available_at, query.as_of, context)
        ]
        return _limited_page(
            page,
            sorted(items, key=lambda item: (item.observed_at, item.observation_id)),
            query.limit,
        )

    async def fetch_macro_series(
        self, query: MacroSeriesQuery, context: FetchContext
    ) -> ProviderPage[MacroSeries]:
        page = self._page_for_query(Dataset.MACRO_SERIES, self._parse_macro_series, query, context)
        items = [
            item
            for item in page.items
            if Region.US in query.regions
            and (not query.series_ids or item.series_id in query.series_ids)
            and item.source.retrieved_at <= context.as_of
        ]
        return _limited_page(page, sorted(items, key=lambda item: item.series_id), query.limit)

    async def fetch_macro_observations(
        self, query: MacroObservationQuery, context: FetchContext
    ) -> ProviderPage[MacroObservation]:
        page = self._page_for_query(
            Dataset.MACRO_OBSERVATIONS,
            self._parse_macro_observation,
            query,
            context,
        )
        items = [
            item
            for item in page.items
            if item.series_id in query.series_ids
            and query.period_from <= item.period_start <= query.period_to
            and _is_available(item.available_at, query.as_of, context)
        ]
        selected_revisions = _select_macro_revisions(items, query.revision_policy)
        return _limited_page(
            page,
            sorted(
                selected_revisions,
                key=lambda item: (item.period_end, item.series_id, item.available_at),
            ),
            query.limit,
        )

    async def fetch_macro_releases(
        self, query: MacroReleaseQuery, context: FetchContext
    ) -> ProviderPage[MacroRelease]:
        page = self._page_for_query(
            Dataset.MACRO_RELEASES,
            self._parse_macro_release,
            query,
            context,
        )
        items = [
            item
            for item in page.items
            if Region.US in query.regions
            and query.scheduled_from <= item.scheduled_at < query.scheduled_to
            and _is_available(item.available_at, query.as_of, context)
        ]
        return _limited_page(
            page, sorted(items, key=lambda item: (item.scheduled_at, item.release_id)), query.limit
        )

    async def fetch_news(self, query: NewsQuery, context: FetchContext) -> ProviderPage[NewsEvent]:
        page = self._page_for_query(Dataset.NEWS, self._parse_news, query, context)
        items = [
            item
            for item in page.items
            if Region.US in query.regions
            and query.published_from <= item.published_at < query.published_to
            and _is_available(item.available_at, query.as_of, context)
            and (not query.languages or item.language in query.languages)
            and (not query.source_tiers or item.source_tier in query.source_tiers)
            and (
                not query.entity_ids
                or bool(
                    set(query.entity_ids).intersection(entity.entity_id for entity in item.entities)
                )
            )
            and (not query.topics or bool(set(query.topics).intersection(item.topics)))
            and (query.include_superseded or item.supersedes_news_id is None)
            and _content_satisfies(query.content_mode, item.content_mode)
        ]
        return _limited_page(
            page,
            sorted(items, key=lambda item: (item.published_at, item.news_id), reverse=True),
            query.limit,
        )

    def _page_for_query[T: StrictModel](
        self,
        dataset: Dataset,
        parser: Callable[[Mapping[str, object]], T],
        query: StrictModel,
        context: FetchContext,
    ) -> ProviderPage[T]:
        fingerprint = _cursor_fingerprint(query, context)
        return self._page(
            dataset,
            parser,
            cursor=_decode_cursor(query.model_dump().get("cursor"), dataset, fingerprint),
            cursor_fingerprint=fingerprint,
        )

    def _page[T: StrictModel](
        self,
        dataset: Dataset,
        parser: Callable[[Mapping[str, object]], T],
        *,
        cursor: str | None,
        cursor_fingerprint: str,
    ) -> ProviderPage[T]:
        payload = self._payload()
        _ensure_keys(payload, _ENVELOPE_KEYS, {"status", "fetched_at", "pages"}, "$")
        pages = _mapping(_required(payload, "pages", "pages"), "pages")
        _ensure_keys(pages, _DATASET_PAGE_KEYS, {dataset.value}, "pages")
        raw_page = _fixture_page(pages, dataset, cursor)
        raw_items = _list(
            _required(raw_page, "items", f"pages.{dataset.value}.items"),
            f"pages.{dataset.value}.items",
        )
        next_cursor = _optional_str(raw_page, "next_cursor")
        if not raw_items and next_cursor is not None:
            raise ProviderCursorError(
                f"empty fixture page for {dataset.value} must not advance to another cursor",
                code="INVALID_PAGINATION",
            )
        self._ensure_no_duplicate_records(raw_items, dataset)
        items: list[T] = []
        warnings: list[WarningItem] = []
        for index, raw_item in enumerate(raw_items):
            try:
                item = parser(_mapping(raw_item, f"pages.{dataset.value}.items[{index}]"))
            except ProviderSchemaError as exc:
                warnings.append(_quarantine_warning(dataset, index, exc))
            except (TimezoneRequiredError, UsNormalizationError, ValueError) as exc:
                warnings.append(
                    _quarantine_warning(
                        dataset,
                        index,
                        ProviderSchemaError(f"invalid {dataset.value} fixture: {exc}"),
                    )
                )
            else:
                items.append(item)
        if raw_items and not items:
            raise ProviderSchemaError(f"all records in {dataset.value} fixture page were rejected")
        return ProviderPage[T](
            items=items,
            next_cursor=(
                _encode_cursor(dataset, next_cursor, cursor_fingerprint)
                if next_cursor is not None
                else None
            ),
            source_watermark=_optional_str(payload, "source_watermark"),
            fetched_at=_datetime(_required(payload, "fetched_at", "fetched_at"), "fetched_at"),
            complete=next_cursor is None,
            warnings=warnings,
        )

    def _ensure_no_duplicate_records(self, raw_items: Sequence[object], dataset: Dataset) -> None:
        record_ids = [
            record_id
            for item in raw_items
            if isinstance(item, dict)
            and isinstance((record_id := item.get("record_id")), str)
            and record_id.strip()
        ]
        if len(record_ids) != len(set(record_ids)):
            raise ProviderCursorError(f"duplicate record id in {dataset.value} fixture page")

    def _payload(self) -> Mapping[str, object]:
        try:
            text = self._fixture_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProviderUnavailableError(f"fixture unavailable: {self._fixture_path}") from exc
        if "<html" in text[:200].lower():
            raise ProviderAuthorizationError(
                "provider returned a login, auth-wall, or risk-control page"
            )
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderSchemaError("provider payload must be valid JSON") from exc
        payload = _mapping(decoded, "$")
        self._raise_for_status(payload)
        return payload

    def _raise_for_status(self, payload: Mapping[str, object]) -> None:
        status = _str(_required(payload, "status", "status"), "status")
        if status == "ok":
            return
        _ensure_keys(payload, _ERROR_ENVELOPE_KEYS, {"status", "error"}, "$")
        if status != "error":
            raise ProviderSchemaError("provider payload status must be ok or error")
        error = _mapping(_required(payload, "error", "error"), "error")
        _ensure_keys(error, _ERROR_KEYS, {"code", "message"}, "error")
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
        if code in {"schema_changed", "schema_drift"}:
            raise ProviderSchemaError(message)
        if code == "invalid_cursor":
            raise ProviderCursorError(message)
        raise ProviderUnavailableError(message)

    def _parse_instrument(self, raw: Mapping[str, object]) -> Instrument:
        _ensure_keys(
            raw,
            _INSTRUMENT_KEYS,
            _INSTRUMENT_KEYS
            - {
                "issuer_key",
                "listed_on",
                "delisted_on",
                "lot_size",
                "valid_to",
                "provider_updated_at",
            },
            "instrument",
        )
        alias = _us_alias(raw, "instrument")
        return Instrument(
            instrument_id=alias.instrument_id,
            canonical_symbol=alias.canonical_symbol,
            region=Region.US,
            venue_mic=alias.venue_mic,
            local_symbol=alias.local_symbol,
            name=_str(_required(raw, "name", "instrument.name"), "instrument.name"),
            asset_class=AssetClass(
                _str(
                    _required(raw, "asset_class", "instrument.asset_class"),
                    "instrument.asset_class",
                )
            ),
            currency=_str(_required(raw, "currency", "instrument.currency"), "instrument.currency"),
            timezone=_str(_required(raw, "timezone", "instrument.timezone"), "instrument.timezone"),
            status=InstrumentStatus(
                _str(_required(raw, "status", "instrument.status"), "instrument.status")
            ),
            listed_on=_optional_date(raw, "listed_on"),
            delisted_on=_optional_date(raw, "delisted_on"),
            lot_size=_optional_decimal(raw, "lot_size"),
            valid_from=alias.valid_from,
            valid_to=alias.valid_to,
            source=_source(raw, source_symbol=alias.source_symbol),
        )

    def _parse_bar(self, raw: Mapping[str, object]) -> MarketBar:
        _ensure_keys(
            raw,
            _BAR_KEYS,
            _BAR_KEYS - {"issuer_key", "volume", "turnover", "provider_updated_at"},
            "bar",
        )
        alias = _us_alias(raw, "bar")
        bar_start = _datetime(_required(raw, "bar_start", "bar.bar_start"), "bar.bar_start")
        bar_end = _datetime(_required(raw, "bar_end", "bar.bar_end"), "bar.bar_end")
        expected_trading_date = _date(
            _required(raw, "trading_date", "bar.trading_date"), "bar.trading_date"
        )
        if us_trading_date(bar_start) != expected_trading_date:
            raise ProviderSchemaError(
                "bar trading_date must be the New York market date of bar_start"
            )
        currency = _str(_required(raw, "currency", "bar.currency"), "bar.currency")
        interval = Interval(_str(_required(raw, "interval", "bar.interval"), "bar.interval"))
        adjustment = Adjustment(
            _str(_required(raw, "adjustment", "bar.adjustment"), "bar.adjustment")
        )
        source = _source(raw, source_symbol=alias.source_symbol)
        return MarketBar(
            bar_id=_stable_id(
                "bar_us",
                alias.instrument_id,
                interval.value,
                bar_start,
                adjustment.value,
                source.provider_id,
            ),
            instrument_id=alias.instrument_id,
            canonical_symbol=alias.canonical_symbol,
            region=Region.US,
            interval=interval,
            bar_start=bar_start,
            bar_end=bar_end,
            trading_date=expected_trading_date,
            open=_currency_value(raw, "open", currency),
            high=_currency_value(raw, "high", currency),
            low=_currency_value(raw, "low", currency),
            close=_currency_value(raw, "close", currency),
            volume=_optional_decimal(raw, "volume"),
            turnover=_optional_decimal(raw, "turnover"),
            currency=currency,
            adjustment=adjustment,
            available_at=_datetime(
                _required(raw, "available_at", "bar.available_at"), "bar.available_at"
            ),
            availability_basis=AvailabilityBasis(
                _str(
                    _required(raw, "availability_basis", "bar.availability_basis"),
                    "bar.availability_basis",
                )
            ),
            source=source,
            quality_flags=_str_list(
                _required(raw, "quality_flags", "bar.quality_flags"), "bar.quality_flags"
            ),
        )

    def _parse_market_observation(self, raw: Mapping[str, object]) -> MarketObservation:
        _ensure_keys(
            raw,
            _MARKET_OBSERVATION_KEYS,
            _MARKET_OBSERVATION_KEYS - {"value", "currency", "provider_updated_at"},
            "market_observation",
        )
        unit_value = _unit_value(raw, "value", "unit", value_optional=True)
        source = _source(raw)
        observed_at = _datetime(
            _required(raw, "observed_at", "market_observation.observed_at"),
            "market_observation.observed_at",
        )
        return MarketObservation(
            observation_id=_stable_id(
                "obs_us",
                Region.US.value,
                raw["scope_id"],
                raw["metric_code"],
                observed_at,
                source.provider_id,
            ),
            region=Region.US,
            scope_type=ScopeType(
                _str(
                    _required(raw, "scope_type", "market_observation.scope_type"),
                    "market_observation.scope_type",
                )
            ),
            scope_id=_str(
                _required(raw, "scope_id", "market_observation.scope_id"),
                "market_observation.scope_id",
            ),
            metric_code=_str(
                _required(raw, "metric_code", "market_observation.metric_code"),
                "market_observation.metric_code",
            ),
            value=unit_value.value,
            unit=unit_value.unit,
            currency=_optional_str(raw, "currency") or unit_value.currency,
            period_start=_datetime(
                _required(raw, "period_start", "market_observation.period_start"),
                "market_observation.period_start",
            ),
            period_end=_datetime(
                _required(raw, "period_end", "market_observation.period_end"),
                "market_observation.period_end",
            ),
            observed_at=observed_at,
            available_at=_datetime(
                _required(raw, "available_at", "market_observation.available_at"),
                "market_observation.available_at",
            ),
            availability_basis=AvailabilityBasis(
                _str(
                    _required(raw, "availability_basis", "market_observation.availability_basis"),
                    "market_observation.availability_basis",
                )
            ),
            dimensions=_str_mapping(
                _required(raw, "dimensions", "market_observation.dimensions"),
                "market_observation.dimensions",
            ),
            source=source,
            quality_flags=_str_list(
                _required(raw, "quality_flags", "market_observation.quality_flags"),
                "market_observation.quality_flags",
            ),
        )

    def _parse_macro_series(self, raw: Mapping[str, object]) -> MacroSeries:
        _ensure_keys(
            raw,
            _MACRO_SERIES_KEYS,
            _MACRO_SERIES_KEYS - {"description", "provider_updated_at"},
            "macro_series",
        )
        return MacroSeries(
            series_id=_str(
                _required(raw, "series_id", "macro_series.series_id"), "macro_series.series_id"
            ),
            region=Region.US,
            authority=_str(
                _required(raw, "authority", "macro_series.authority"), "macro_series.authority"
            ),
            code=_str(_required(raw, "code", "macro_series.code"), "macro_series.code"),
            name=_str(_required(raw, "name", "macro_series.name"), "macro_series.name"),
            description=_optional_str(raw, "description"),
            frequency=Frequency(
                _str(
                    _required(raw, "frequency", "macro_series.frequency"), "macro_series.frequency"
                )
            ),
            unit=_unit_value(raw, "value", "unit", value_optional=True).unit,
            transformation=cast(
                Any,
                _str(
                    _required(raw, "transformation", "macro_series.transformation"),
                    "macro_series.transformation",
                ),
            ),
            seasonal_adjustment=cast(
                Any,
                _str(
                    _required(raw, "seasonal_adjustment", "macro_series.seasonal_adjustment"),
                    "macro_series.seasonal_adjustment",
                ),
            ),
            source=_source(raw),
        )

    def _parse_macro_observation(self, raw: Mapping[str, object]) -> MacroObservation:
        _ensure_keys(
            raw,
            _MACRO_OBSERVATION_KEYS,
            _MACRO_OBSERVATION_KEYS
            - {"value", "released_at", "supersedes_observation_id", "provider_updated_at"},
            "macro_observation",
        )
        unit_value = _unit_value(raw, "value", "unit")
        series_id = _str(
            _required(raw, "series_id", "macro_observation.series_id"),
            "macro_observation.series_id",
        )
        period_end = _date(
            _required(raw, "period_end", "macro_observation.period_end"),
            "macro_observation.period_end",
        )
        vintage_id = _str(
            _required(raw, "vintage_id", "macro_observation.vintage_id"),
            "macro_observation.vintage_id",
        )
        source = _source(raw)
        return MacroObservation(
            observation_id=_stable_id(
                "mobs_us", series_id, period_end, vintage_id, source.provider_id
            ),
            series_id=series_id,
            region=Region.US,
            period_start=_date(
                _required(raw, "period_start", "macro_observation.period_start"),
                "macro_observation.period_start",
            ),
            period_end=period_end,
            value=unit_value.value,
            unit=unit_value.unit,
            transformation=_str(
                _required(raw, "transformation", "macro_observation.transformation"),
                "macro_observation.transformation",
            ),
            released_at=_optional_datetime(raw, "released_at"),
            available_at=_datetime(
                _required(raw, "available_at", "macro_observation.available_at"),
                "macro_observation.available_at",
            ),
            availability_basis=AvailabilityBasis(
                _str(
                    _required(raw, "availability_basis", "macro_observation.availability_basis"),
                    "macro_observation.availability_basis",
                )
            ),
            vintage_id=vintage_id,
            revision_no=_int(
                _required(raw, "revision_no", "macro_observation.revision_no"),
                "macro_observation.revision_no",
            ),
            value_status=cast(
                Any,
                _str(
                    _required(raw, "value_status", "macro_observation.value_status"),
                    "macro_observation.value_status",
                ),
            ),
            supersedes_observation_id=_optional_str(raw, "supersedes_observation_id"),
            source=source,
            quality_flags=_str_list(
                _required(raw, "quality_flags", "macro_observation.quality_flags"),
                "macro_observation.quality_flags",
            ),
        )

    def _parse_macro_release(self, raw: Mapping[str, object]) -> MacroRelease:
        _ensure_keys(
            raw,
            _MACRO_RELEASE_KEYS,
            _MACRO_RELEASE_KEYS
            - {"released_at", "actual", "consensus", "previous", "provider_updated_at"},
            "macro_release",
        )
        series_id = _str(
            _required(raw, "series_id", "macro_release.series_id"), "macro_release.series_id"
        )
        scheduled_at = _datetime(
            _required(raw, "scheduled_at", "macro_release.scheduled_at"),
            "macro_release.scheduled_at",
        )
        period_end = _date(
            _required(raw, "period_end", "macro_release.period_end"), "macro_release.period_end"
        )
        source = _source(raw)
        return MacroRelease(
            release_id=_stable_id(
                "mrel_us", series_id, scheduled_at, period_end, source.provider_id
            ),
            series_id=series_id,
            region=Region.US,
            release_name=_str(
                _required(raw, "release_name", "macro_release.release_name"),
                "macro_release.release_name",
            ),
            scheduled_at=scheduled_at,
            released_at=_optional_datetime(raw, "released_at"),
            available_at=_datetime(
                _required(raw, "available_at", "macro_release.available_at"),
                "macro_release.available_at",
            ),
            period_start=_date(
                _required(raw, "period_start", "macro_release.period_start"),
                "macro_release.period_start",
            ),
            period_end=period_end,
            actual=_unit_value(raw, "actual", "unit", value_optional=True).value,
            consensus=_unit_value(raw, "consensus", "unit", value_optional=True).value,
            previous=_unit_value(raw, "previous", "unit", value_optional=True).value,
            unit=_unit_value(raw, "actual", "unit", value_optional=True).unit,
            status=cast(
                Any, _str(_required(raw, "status", "macro_release.status"), "macro_release.status")
            ),
            source=source,
        )

    def _parse_news(self, raw: Mapping[str, object]) -> NewsEvent:
        _ensure_keys(raw, _NEWS_KEYS, _NEWS_KEYS - {"summary", "provider_updated_at"}, "news")
        rights = _mapping(_required(raw, "rights", "news.rights"), "news.rights")
        _ensure_keys(rights, _RIGHTS_KEYS, _RIGHTS_KEYS - {"content_expires_at"}, "news.rights")
        accession = _str(
            _required(raw, "accession_number", "news.accession_number"), "news.accession_number"
        )
        title = _str(_required(raw, "title", "news.title"), "news.title")
        summary = _optional_str(raw, "summary")
        canonical_url = canonicalize_url(
            _str(_required(raw, "canonical_url", "news.canonical_url"), "news.canonical_url")
        )
        return NewsEvent(
            news_id=f"news_us_sec_{accession.replace('-', '').lower()}",
            title=title,
            summary=summary,
            body=None,
            content_mode=ContentMode.SNIPPET if summary is not None else ContentMode.HEADLINE,
            language=_str(_required(raw, "language", "news.language"), "news.language"),
            source_name=_str(_required(raw, "source_name", "news.source_name"), "news.source_name"),
            source_tier=SourceTier(
                _str(_required(raw, "source_tier", "news.source_tier"), "news.source_tier")
            ),
            canonical_url=canonical_url,
            published_at=_datetime(
                _required(raw, "published_at", "news.published_at"), "news.published_at"
            ),
            first_seen_at=_datetime(
                _required(raw, "first_seen_at", "news.first_seen_at"), "news.first_seen_at"
            ),
            available_at=_datetime(
                _required(raw, "available_at", "news.available_at"), "news.available_at"
            ),
            availability_basis=AvailabilityBasis(
                _str(
                    _required(raw, "availability_basis", "news.availability_basis"),
                    "news.availability_basis",
                )
            ),
            regions=[Region.US],
            entities=_news_entities(_required(raw, "entities", "news.entities")),
            topics=_str_list(_required(raw, "topics", "news.topics"), "news.topics"),
            content_hash_sha256=canonical_json_checksum(
                {"title": title, "summary": summary, "url": canonical_url}
            ),
            usage_rights=UsageRights(
                storage_allowed=_bool(
                    _required(rights, "storage_allowed", "news.rights.storage_allowed"),
                    "news.rights.storage_allowed",
                ),
                internal_analysis_allowed=_bool(
                    _required(
                        rights, "internal_analysis_allowed", "news.rights.internal_analysis_allowed"
                    ),
                    "news.rights.internal_analysis_allowed",
                ),
                external_llm_allowed=_bool(
                    _required(rights, "external_llm_allowed", "news.rights.external_llm_allowed"),
                    "news.rights.external_llm_allowed",
                ),
                embedding_allowed=_bool(
                    _required(rights, "embedding_allowed", "news.rights.embedding_allowed"),
                    "news.rights.embedding_allowed",
                ),
                redistribution_allowed=_bool(
                    _required(
                        rights, "redistribution_allowed", "news.rights.redistribution_allowed"
                    ),
                    "news.rights.redistribution_allowed",
                ),
                content_expires_at=_optional_datetime(rights, "content_expires_at"),
            ),
            source=_source(raw),
            quality_flags=_str_list(
                _required(raw, "quality_flags", "news.quality_flags"), "news.quality_flags"
            ),
        )


def register_us_provider_roles(registry: ProviderRegistry, provider: UsFixtureProvider) -> None:
    registry.register(provider)
    for role, provider_id in US_ROLE_BINDINGS.items():
        registry.bind_role(role, provider_id)


def _cursor_fingerprint(query: StrictModel, context: FetchContext) -> str:
    query_payload = query.model_dump()
    query_payload.pop("cursor", None)
    return canonical_json_checksum(
        {
            "query": _canonical_cursor_value(query_payload),
            "context_as_of": context.as_of.isoformat(),
            "cursor_version": "fixture-v1",
        }
    )


def _canonical_cursor_value(value: object) -> object:
    if isinstance(value, dict):
        return {key: _canonical_cursor_value(item) for key, item in value.items()}
    if isinstance(value, set):
        return sorted(
            (_canonical_cursor_value(item) for item in value),
            key=canonical_json_checksum,
        )
    if isinstance(value, list):
        return [_canonical_cursor_value(item) for item in value]
    return value


def _encode_cursor(dataset: Dataset, raw_cursor: str, fingerprint: str) -> str:
    return f"fixture-v1:{dataset.value}:{fingerprint}:{raw_cursor}"


def _decode_cursor(cursor: object, dataset: Dataset, fingerprint: str) -> str | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str):
        raise ProviderCursorError("fixture cursor must be a string")
    try:
        version, encoded_dataset, encoded_fingerprint, raw_cursor = cursor.split(":", maxsplit=3)
    except ValueError as exc:
        raise ProviderCursorError("fixture cursor is malformed") from exc
    if (
        version != "fixture-v1"
        or encoded_dataset != dataset.value
        or encoded_fingerprint != fingerprint
        or not raw_cursor
    ):
        raise ProviderCursorError("fixture cursor is not valid for this query snapshot")
    return raw_cursor


def _fixture_page(
    pages: Mapping[str, object], dataset: Dataset, cursor: str | None
) -> Mapping[str, object]:
    raw_dataset_page = _required(pages, dataset.value, f"pages.{dataset.value}")
    if isinstance(raw_dataset_page, dict):
        if cursor is not None:
            raise ProviderCursorError(
                f"fixture dataset {dataset.value} has no continuation cursor: {cursor}"
            )
        raw_page = _mapping(raw_dataset_page, f"pages.{dataset.value}")
        _ensure_keys(raw_page, _PAGE_KEYS, _PAGE_KEYS, f"pages.{dataset.value}")
        return raw_page

    raw_page_sequence = _list(raw_dataset_page, f"pages.{dataset.value}")
    for index, raw_page_value in enumerate(raw_page_sequence):
        raw_page = _mapping(raw_page_value, f"pages.{dataset.value}[{index}]")
        _ensure_keys(
            raw_page,
            _PAGINATED_PAGE_KEYS,
            _PAGINATED_PAGE_KEYS,
            f"pages.{dataset.value}[{index}]",
        )
        if _optional_str(raw_page, "cursor") == cursor:
            return raw_page
    raise ProviderCursorError(f"unknown fixture cursor for {dataset.value}: {cursor}")


def _quarantine_warning(dataset: Dataset, index: int, error: ProviderSchemaError) -> WarningItem:
    return WarningItem(
        code="PROVIDER_RECORD_QUARANTINED",
        message=str(error)[:500],
        scope=f"{dataset.value}[{index}]",
    )


def _us_alias(raw: Mapping[str, object], path: str) -> Any:
    identity = UsInstrumentIdentity(
        issuer_key=_optional_str(raw, "issuer_key"),
        first_canonical_symbol=_str(
            _required(raw, "first_canonical_symbol", f"{path}.first_canonical_symbol"),
            f"{path}.first_canonical_symbol",
        ),
        first_valid_from=_date(
            _required(raw, "first_valid_from", f"{path}.first_valid_from"),
            f"{path}.first_valid_from",
        ),
    )
    return normalize_us_alias(
        source_symbol=_str(_required(raw, "symbol", f"{path}.symbol"), f"{path}.symbol"),
        exchange=_str(_required(raw, "exchange", f"{path}.exchange"), f"{path}.exchange"),
        valid_from=_date(_required(raw, "valid_from", f"{path}.valid_from"), f"{path}.valid_from"),
        valid_to=_optional_date(raw, "valid_to"),
        instrument_identity=identity,
    )


def _news_entities(value: object) -> list[EntityRef]:
    entities: list[EntityRef] = []
    for index, raw_entity in enumerate(_list(value, "news.entities")):
        entity = _mapping(raw_entity, f"news.entities[{index}]")
        _ensure_keys(
            entity,
            _ENTITY_KEYS,
            _ENTITY_KEYS - {"mention"},
            f"news.entities[{index}]",
        )
        entities.append(
            EntityRef(
                entity_type=cast(
                    Any,
                    _str(
                        _required(entity, "entity_type", f"news.entities[{index}].entity_type"),
                        f"news.entities[{index}].entity_type",
                    ),
                ),
                entity_id=_str(
                    _required(entity, "entity_id", f"news.entities[{index}].entity_id"),
                    f"news.entities[{index}].entity_id",
                ),
                mention=_optional_str(entity, "mention"),
                confidence=_decimal(
                    _required(entity, "confidence", f"news.entities[{index}].confidence"),
                    f"news.entities[{index}].confidence",
                ),
            )
        )
    return entities


def _source(raw: Mapping[str, object], *, source_symbol: str | None = None) -> SourceRef:
    business_payload = {
        key: value
        for key, value in raw.items()
        if key not in {"retrieved_at", "provider_updated_at"}
    }
    record_id = _str(_required(raw, "record_id", "source.record_id"), "source.record_id")
    return SourceRef(
        provider_id=US_PROVIDER_ID,
        provider_record_id=f"{US_PROVIDER_ID}:{record_id}",
        source_name=_str(_required(raw, "source_name", "source.source_name"), "source.source_name"),
        source_url=_str(_required(raw, "source_url", "source.source_url"), "source.source_url"),
        source_symbol=source_symbol,
        retrieved_at=_datetime(
            _required(raw, "retrieved_at", "source.retrieved_at"), "source.retrieved_at"
        ),
        provider_updated_at=_optional_datetime(raw, "provider_updated_at"),
        checksum_sha256=canonical_json_checksum(business_payload),
    )


def _currency_value(raw: Mapping[str, object], key: str, currency: str) -> Decimal:
    normalized = normalize_us_value(_str(_required(raw, key, key), key), unit_hint=currency)
    if normalized.value is None:
        raise ProviderSchemaError(f"{key} cannot be a missing value")
    return normalized.value


def _unit_value(
    raw: Mapping[str, object],
    value_key: str,
    unit_key: str,
    *,
    value_optional: bool = False,
) -> Any:
    raw_value = (
        _optional_str(raw, value_key)
        if value_optional
        else _str(_required(raw, value_key, value_key), value_key)
    )
    unit = _str(_required(raw, unit_key, unit_key), unit_key)
    if raw_value is None:
        return normalize_us_value("--", unit_hint=unit)
    return normalize_us_value(raw_value, unit_hint=unit)


def _select_macro_revisions(
    items: list[MacroObservation], revision_policy: RevisionPolicy
) -> list[MacroObservation]:
    if revision_policy is RevisionPolicy.ALL_VINTAGES:
        return items

    selected: dict[tuple[str, date], MacroObservation] = {}
    for item in items:
        key = (item.series_id, item.period_end)
        current = selected.get(key)
        if current is None:
            selected[key] = item
            continue
        if revision_policy is RevisionPolicy.FIRST_RELEASE:
            if (item.revision_no, item.available_at, item.observation_id) < (
                current.revision_no,
                current.available_at,
                current.observation_id,
            ):
                selected[key] = item
        elif (item.revision_no, item.available_at, item.observation_id) > (
            current.revision_no,
            current.available_at,
            current.observation_id,
        ):
            selected[key] = item
    return list(selected.values())


def _limited_page[T: StrictModel](
    page: ProviderPage[T], items: list[T], limit: int
) -> ProviderPage[T]:
    if len(items) > limit:
        raise UnsupportedCapabilityError(
            "fixture page exceeds query limit; request a larger limit or use an approved "
            "fixture cursor"
        )
    return ProviderPage[T](
        items=items,
        next_cursor=page.next_cursor,
        source_watermark=page.source_watermark,
        fetched_at=page.fetched_at,
        complete=page.complete,
        warnings=page.warnings,
    )


def _is_active_on(item: Instrument, active_on: date) -> bool:
    return item.valid_from <= active_on and (item.valid_to is None or active_on < item.valid_to)


def _is_available(available_at: datetime, query_as_of: datetime, context: FetchContext) -> bool:
    return available_at <= query_as_of and available_at <= context.as_of


def _content_satisfies(requested: ContentMode, actual: ContentMode) -> bool:
    if requested is ContentMode.FULL_TEXT:
        return actual is ContentMode.FULL_TEXT
    if requested is ContentMode.SNIPPET:
        return actual in {ContentMode.SNIPPET, ContentMode.FULL_TEXT}
    return True


def _stable_id(namespace: str, *parts: object) -> str:
    seed = "\x1f".join(str(part) for part in parts)
    return f"{namespace}_{sha256(seed.encode('utf-8')).hexdigest()[:20]}"


def _ensure_keys(
    raw: Mapping[str, object],
    allowed: frozenset[str],
    required: frozenset[str] | set[str],
    path: str,
) -> None:
    unknown = set(raw) - allowed
    missing = required - set(raw)
    if unknown:
        raise ProviderSchemaError(f"unexpected fields at {path}: {sorted(unknown)}")
    if missing:
        raise ProviderSchemaError(f"missing fields at {path}: {sorted(missing)}")


def _required(raw: Mapping[str, object], key: str, path: str) -> object:
    if key not in raw or raw[key] is None:
        raise ProviderSchemaError(f"missing required field: {path}")
    return raw[key]


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ProviderSchemaError(f"{path} must be an object")
    return cast(Mapping[str, object], value)


def _list(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        raise ProviderSchemaError(f"{path} must be an array")
    return cast(list[object], value)


def _str(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ProviderSchemaError(f"{path} must be a string")
    if not value.strip():
        raise ProviderSchemaError(f"{path} must not be empty")
    return value


def _optional_str(raw: Mapping[str, object], key: str) -> str | None:
    if key not in raw or raw[key] is None:
        return None
    return _str(raw[key], key)


def _bool(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ProviderSchemaError(f"{path} must be a boolean")
    return value


def _int(value: object, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProviderSchemaError(f"{path} must be an integer")
    return value


def _optional_int(raw: Mapping[str, object], key: str) -> int | None:
    if key not in raw or raw[key] is None:
        return None
    return _int(raw[key], key)


def _date(value: object, path: str) -> date:
    if not isinstance(value, str):
        raise ProviderSchemaError(f"{path} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ProviderSchemaError(f"{path} must be an ISO date") from exc


def _optional_date(raw: Mapping[str, object], key: str) -> date | None:
    if key not in raw or raw[key] is None:
        return None
    return _date(raw[key], key)


def _datetime(value: object, path: str) -> datetime:
    if not isinstance(value, str):
        raise ProviderSchemaError(f"{path} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderSchemaError(f"{path} must be an ISO datetime") from exc
    return to_us_market_utc(parsed).astimezone(UTC)


def _optional_datetime(raw: Mapping[str, object], key: str) -> datetime | None:
    if key not in raw or raw[key] is None:
        return None
    return _datetime(raw[key], key)


def _decimal(value: object, path: str) -> Decimal:
    if not isinstance(value, str):
        raise ProviderSchemaError(f"{path} must be a decimal string")
    try:
        return Decimal(value)
    except Exception as exc:
        raise ProviderSchemaError(f"{path} must be a decimal string") from exc


def _optional_decimal(raw: Mapping[str, object], key: str) -> Decimal | None:
    value = _optional_str(raw, key)
    if value is None:
        return None
    return _decimal(value, key)


def _str_list(value: object, path: str) -> list[str]:
    values = _list(value, path)
    return [_str(item, path) for item in values]


def _str_mapping(value: object, path: str) -> dict[str, str]:
    mapping = _mapping(value, path)
    return {_str(key, path): _str(item, path) for key, item in mapping.items()}
