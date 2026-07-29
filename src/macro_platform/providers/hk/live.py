from __future__ import annotations

from calendar import monthrange
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any, ClassVar, Literal
from urllib.parse import urljoin

import httpx

from macro_platform.contracts.common import AvailabilityBasis, Region, UsageRights
from macro_platform.contracts.macro import (
    Frequency,
    MacroObservation,
    MacroObservationQuery,
    MacroSeries,
    MacroSeriesQuery,
    RevisionPolicy,
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
from macro_platform.providers.base import (
    ProviderCursorError,
    ProviderSchemaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    UnsupportedCapabilityError,
)
from macro_platform.providers.live import (
    LiveHttpProvider,
    assert_cursor_snapshot,
    assert_cursor_snapshot_at,
    health_status_for_error,
    source_ref,
    stable_provider_record_id,
)

HK_CSD_API_URL = "https://www.censtatd.gov.hk/api/get.php"
HK_CSD_ALLOWED_HOSTS = frozenset({"www.censtatd.gov.hk"})
HK_CSD_DATASET_ALLOWLIST = frozenset({"510-60004"})
HK_CSD_PROVIDER_ID = "hk.censtatd.v1"
HKMA_PRESS_RELEASES_URL = "https://api.hkma.gov.hk/public/press-releases"
HKMA_PRESS_RELEASES_LISTING_URL = "https://www.hkma.gov.hk/eng/news-and-media/press-releases/"
HKMA_ALLOWED_HOSTS = frozenset({"api.hkma.gov.hk"})
HKMA_PRESS_RELEASE_PROVIDER_ID = "hk.hkma.press-releases.v1"
_MAX_API_PAGE_SIZE = 100


@dataclass(frozen=True)
class _CsdSeriesDefinition:
    name: str
    source_description: str
    source_frequency: str
    frequency: Frequency
    unit: str
    transformation: Literal["level", "mom", "qoq", "yoy", "annualized", "index"]
    seasonal_adjustment: Literal["adjusted", "not_adjusted", "unknown"]


# C&SD dataset 510-60004 is deliberately narrow: new ``sv`` values require
# an allowlist review before they can become canonical platform series.  The
# endpoint exposes four CPI expenditure groups as distinct ``sv`` values, so
# each needs its own canonical series identity; combining them would corrupt
# the observation history.
HK_CSD_SERIES_REGISTRY = {
    "SCC_CM": _CsdSeriesDefinition(
        name="Composite CPI average monthly change (latest three months)",
        source_description="Average monthly rate of change during the latest 3 months (%)",
        source_frequency="M",
        frequency=Frequency.MONTHLY,
        unit="percent",
        transformation="mom",
        seasonal_adjustment="adjusted",
    ),
    "SA_CM": _CsdSeriesDefinition(
        name="CPI(A) average monthly change (latest three months)",
        source_description="Average monthly rate of change during the latest 3 months (%)",
        source_frequency="M",
        frequency=Frequency.MONTHLY,
        unit="percent",
        transformation="mom",
        seasonal_adjustment="adjusted",
    ),
    "SB_CM": _CsdSeriesDefinition(
        name="CPI(B) average monthly change (latest three months)",
        source_description="Average monthly rate of change during the latest 3 months (%)",
        source_frequency="M",
        frequency=Frequency.MONTHLY,
        unit="percent",
        transformation="mom",
        seasonal_adjustment="adjusted",
    ),
    "SC_CM": _CsdSeriesDefinition(
        name="CPI(C) average monthly change (latest three months)",
        source_description="Average monthly rate of change during the latest 3 months (%)",
        source_frequency="M",
        frequency=Frequency.MONTHLY,
        unit="percent",
        transformation="mom",
        seasonal_adjustment="adjusted",
    ),
}


class HkCsdProvider(LiveHttpProvider):
    provider_id: ClassVar[str] = HK_CSD_PROVIDER_ID
    source_name: ClassVar[str] = "Hong Kong Census and Statistics Department open data"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = HK_CSD_API_URL,
        dataset_id: str = "510-60004",
        language: str = "en",
        timeout_seconds: float = 30,
        clock: Callable[[], datetime] | None = None,
        cursor_signing_secret: str | None = None,
    ) -> None:
        super().__init__(
            client=client,
            base_url=base_url,
            allowed_hosts=HK_CSD_ALLOWED_HOSTS,
            timeout_seconds=timeout_seconds,
            clock=clock or _utc_now,
            cursor_signing_secret=cursor_signing_secret,
        )
        if dataset_id not in HK_CSD_DATASET_ALLOWLIST:
            raise ValueError("C&SD dataset is outside the approved allowlist")
        self.dataset_id = dataset_id
        self.language = language

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            regions={Region.HK},
            datasets={Dataset.MACRO_SERIES, Dataset.MACRO_OBSERVATIONS},
            max_page_size=1000,
            supports_point_in_time=False,
            # The public C&SD response exposes the current value, but no
            # revision sequence or vintage history. Keep the value checksum
            # stable for identity without claiming revision support.
            supports_revisions=False,
            supports_full_text=False,
            external_llm_allowed=True,
        )

    async def healthcheck(self) -> ProviderHealth:
        checked_at = self._clock().astimezone(UTC)
        try:
            await self._fetch_dataset(checked_at + _duration_seconds(self._timeout_seconds))
        except Exception as exc:  # noqa: BLE001 - health must not take the process down
            return ProviderHealth(
                provider_id=self.provider_id,
                status=health_status_for_error(exc),
                checked_at=checked_at,
                latency_ms=0,
                message=type(exc).__name__,
            )
        return ProviderHealth(
            provider_id=self.provider_id,
            status="ok",
            checked_at=checked_at,
            latency_ms=0,
        )

    async def fetch_macro_series(
        self, query: MacroSeriesQuery, context: FetchContext
    ) -> ProviderPage[MacroSeries]:
        if Region.HK not in query.regions:
            return _empty_page(self._clock)
        _ensure_requested_series_are_registered(query.series_ids, self.dataset_id)
        fingerprint = self._cursor_fingerprint(query, context)
        offset, last_record_key, snapshot_at, snapshot_watermark = self._decode_cursor(
            query.cursor, fingerprint
        )
        payload, response, fetched_at = await self._fetch_dataset(context.deadline_at)
        rows = _dataset_rows(payload)
        source_watermark = canonical_json_checksum({"dataset_id": self.dataset_id, "dataSet": rows})
        assert_cursor_snapshot(snapshot_watermark, source_watermark)
        assert_cursor_snapshot_at(snapshot_at, fetched_at)
        series_by_id: dict[str, MacroSeries] = {}
        for row in rows:
            sv, _definition = _series_definition(row)
            series_id = _series_id(self.dataset_id, sv)
            if series_id in series_by_id:
                continue
            series_by_id[series_id] = self._series_from_row(
                row, fetched_at=fetched_at, source_url=str(response.url)
            )
        if not series_by_id:
            raise ProviderSchemaError("C&SD response has no series rows", code="SCHEMA_DRIFT")
        all_items = sorted(series_by_id.values(), key=lambda item: item.series_id)
        if query.series_ids:
            all_items = [item for item in all_items if item.series_id in set(query.series_ids)]
        if offset > 0 and (
            offset > len(all_items) or all_items[offset - 1].series_id != last_record_key
        ):
            raise ProviderCursorError(
                "C&SD cursor predecessor does not match the source snapshot",
                code="SNAPSHOT_CHANGED",
            )
        if offset > len(all_items):
            raise ProviderCursorError("C&SD cursor is past the result set", code="INVALID_CURSOR")
        page_size = min(query.limit, self.capabilities().max_page_size)
        items = all_items[offset : offset + page_size]
        has_more = offset + len(items) < len(all_items)
        next_cursor = (
            self._encode_cursor(
                offset=offset + len(items),
                fingerprint=fingerprint,
                snapshot_at=fetched_at.isoformat(),
                snapshot_watermark=source_watermark,
                last_record_key=items[-1].series_id if items else None,
            )
            if has_more
            else None
        )
        return ProviderPage(
            items=items,
            next_cursor=next_cursor,
            source_watermark=source_watermark,
            fetched_at=fetched_at,
            complete=not has_more,
        )

    async def fetch_macro_observations(
        self, query: MacroObservationQuery, context: FetchContext
    ) -> ProviderPage[MacroObservation]:
        _ensure_requested_series_are_registered(query.series_ids, self.dataset_id)
        if query.revision_policy is not RevisionPolicy.LATEST_AS_OF:
            raise UnsupportedCapabilityError(
                "C&SD observations do not expose first-release or all-vintage history"
            )
        fingerprint = self._cursor_fingerprint(query, context)
        offset, last_record_key, snapshot_at, snapshot_watermark = self._decode_cursor(
            query.cursor, fingerprint
        )
        payload, response, fetched_at = await self._fetch_dataset(context.deadline_at)
        rows = _dataset_rows(payload)
        source_watermark = canonical_json_checksum({"dataset_id": self.dataset_id, "dataSet": rows})
        assert_cursor_snapshot(snapshot_watermark, source_watermark)
        assert_cursor_snapshot_at(snapshot_at, fetched_at)
        if fetched_at > query.as_of:
            raise UnsupportedCapabilityError(
                "C&SD observations do not provide historical point-in-time snapshots"
            )
        requested = set(query.series_ids)
        observations: list[MacroObservation] = []
        for row in rows:
            sv, definition = _series_definition(row)
            series_id = _series_id(self.dataset_id, sv)
            if series_id not in requested:
                continue
            period = _required_text(row, "period", "dataSet.period")
            period_start, period_end = _period_bounds(period)
            if not query.period_from <= period_start <= query.period_to:
                continue
            value = _optional_decimal(row.get("figure"), "dataSet.figure")
            record_id = stable_provider_record_id("hk-csd-observation", self.dataset_id, sv, period)
            value_payload = {
                "sv": sv,
                "figure": row.get("figure"),
                "sd_value": row.get("sd_value"),
            }
            vintage_id = f"{series_id}:{canonical_json_checksum(value_payload)}"
            observations.append(
                MacroObservation(
                    observation_id=stable_provider_record_id(
                        "obs", series_id, period_start, period_end, vintage_id, record_id
                    ),
                    series_id=series_id,
                    region=Region.HK,
                    period_start=period_start,
                    period_end=period_end,
                    value=value,
                    unit=definition.unit,
                    transformation=definition.transformation,
                    released_at=None,
                    available_at=fetched_at,
                    availability_basis=AvailabilityBasis.FIRST_SEEN,
                    vintage_id=vintage_id,
                    revision_no=0,
                    value_status="preliminary",
                    source=source_ref(
                        provider_id=self.provider_id,
                        provider_record_id=record_id,
                        source_name=self.source_name,
                        source_url=str(response.url),
                        retrieved_at=fetched_at,
                        checksum_payload={
                            "dataset_id": self.dataset_id,
                            "sv": sv,
                            "period": period,
                            "figure": row.get("figure"),
                        },
                    ),
                )
            )
        observations.sort(key=lambda item: (item.period_start, item.observation_id))
        if offset > 0 and (
            offset > len(observations) or observations[offset - 1].observation_id != last_record_key
        ):
            raise ProviderCursorError(
                "C&SD cursor predecessor does not match the source snapshot",
                code="SNAPSHOT_CHANGED",
            )
        if offset > len(observations):
            raise ProviderCursorError("C&SD cursor is past the result set", code="INVALID_CURSOR")
        page_size = min(query.limit, self.capabilities().max_page_size)
        items = observations[offset : offset + page_size]
        has_more = offset + len(items) < len(observations)
        next_cursor = (
            self._encode_cursor(
                offset=offset + len(items),
                fingerprint=fingerprint,
                snapshot_at=fetched_at.isoformat(),
                snapshot_watermark=source_watermark,
                last_record_key=items[-1].observation_id if items else None,
            )
            if has_more
            else None
        )
        return ProviderPage(
            items=items,
            next_cursor=next_cursor,
            source_watermark=source_watermark,
            fetched_at=fetched_at,
            complete=not has_more,
        )

    async def _fetch_dataset(
        self, deadline_at: datetime
    ) -> tuple[dict[str, Any], httpx.Response, datetime]:
        payload, response, fetched_at = await self._get_json(
            params={"id": self.dataset_id, "lang": self.language, "full_series": 1},
            context_deadline=deadline_at,
        )
        header = _mapping(payload.get("header"), "header")
        status = _mapping(header.get("status"), "header.status")
        if status.get("code") not in {0, "0"} or str(status.get("name", "")).lower() != "success":
            raise ProviderSchemaError("C&SD response reports failure", code="UPSTREAM_SCHEMA_ERROR")
        return payload, response, _server_finished_at(header, fetched_at)

    def _series_from_row(
        self, row: Mapping[str, Any], *, fetched_at: datetime, source_url: str
    ) -> MacroSeries:
        sv = _required_text(row, "sv", "dataSet.sv")
        description = _required_text(row, "svDesc", "dataSet.svDesc")
        _sv, definition = _series_definition(row)
        return MacroSeries(
            series_id=_series_id(self.dataset_id, sv),
            region=Region.HK,
            authority="CENSTATD",
            code=f"{self.dataset_id}:{sv}",
            name=definition.name,
            description=definition.source_description,
            frequency=definition.frequency,
            unit=definition.unit,
            transformation=definition.transformation,
            seasonal_adjustment=definition.seasonal_adjustment,
            source=source_ref(
                provider_id=self.provider_id,
                provider_record_id=f"{self.dataset_id}:{sv}",
                source_name=self.source_name,
                source_url=source_url,
                retrieved_at=fetched_at,
                checksum_payload={
                    "dataset_id": self.dataset_id,
                    "sv": sv,
                    "description": description,
                },
            ),
        )


class HkmaPressReleaseProvider(LiveHttpProvider):
    provider_id: ClassVar[str] = HKMA_PRESS_RELEASE_PROVIDER_ID
    source_name: ClassVar[str] = "Hong Kong Monetary Authority press releases"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = HKMA_PRESS_RELEASES_URL,
        timeout_seconds: float = 30,
        clock: Callable[[], datetime] | None = None,
        cursor_signing_secret: str | None = None,
    ) -> None:
        super().__init__(
            client=client,
            base_url=base_url,
            allowed_hosts=HKMA_ALLOWED_HOSTS,
            timeout_seconds=timeout_seconds,
            clock=clock or _utc_now,
            cursor_signing_secret=cursor_signing_secret,
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            regions={Region.HK},
            datasets={Dataset.NEWS},
            max_page_size=_MAX_API_PAGE_SIZE,
            supports_point_in_time=False,
            supports_revisions=False,
            supports_full_text=False,
            external_llm_allowed=True,
        )

    async def healthcheck(self) -> ProviderHealth:
        checked_at = self._clock().astimezone(UTC)
        try:
            await self._fetch_page(
                offset=0, context_deadline=checked_at + _duration_seconds(self._timeout_seconds)
            )
        except Exception as exc:  # noqa: BLE001 - health must not take the process down
            return ProviderHealth(
                provider_id=self.provider_id,
                status=health_status_for_error(exc),
                checked_at=checked_at,
                latency_ms=0,
                message=type(exc).__name__,
            )
        return ProviderHealth(
            provider_id=self.provider_id,
            status="ok",
            checked_at=checked_at,
            latency_ms=0,
        )

    async def fetch_news(self, query: NewsQuery, context: FetchContext) -> ProviderPage[NewsEvent]:
        if Region.HK not in query.regions:
            return ProviderPage(items=[], fetched_at=self._clock().astimezone(UTC), complete=True)
        if query.content_mode is ContentMode.FULL_TEXT:
            raise UnsupportedCapabilityError(
                "HKMA press release adapter only exposes headline metadata"
            )
        fingerprint = self._cursor_fingerprint(query, context)
        offset, previous_record_key, snapshot_at, snapshot_watermark = self._decode_cursor(
            query.cursor, fingerprint
        )
        raw_records, source_url, fetched_at, source_watermark = await self._fetch_snapshot(
            context_deadline=context.deadline_at
        )
        assert_cursor_snapshot(snapshot_watermark, source_watermark)
        assert_cursor_snapshot_at(snapshot_at, fetched_at)
        if query.as_of < fetched_at:
            raise UnsupportedCapabilityError(
                "HKMA press releases do not provide historical point-in-time snapshots"
            )
        items = [
            _news_event(
                record,
                fetched_at=fetched_at,
                source_url=source_url,
                provider_id=self.provider_id,
                source_name=self.source_name,
            )
            for record in raw_records
        ]
        items = [
            item
            for item in items
            if _news_overlaps_query_window(item, query) and item.available_at <= query.as_of
        ]
        items.sort(key=lambda item: (_news_sort_at(item), item.news_id), reverse=True)
        if offset > 0 and (offset > len(items) or items[offset - 1].news_id != previous_record_key):
            raise ProviderCursorError(
                "HKMA cursor predecessor does not match the source snapshot",
                code="SNAPSHOT_CHANGED",
            )
        if offset > len(items):
            raise ProviderCursorError("HKMA cursor is past the result set", code="INVALID_CURSOR")
        page_size = min(query.limit, _MAX_API_PAGE_SIZE)
        page_items = items[offset : offset + page_size]
        has_more = offset + len(page_items) < len(items)
        next_cursor = (
            self._encode_cursor(
                offset=offset + len(page_items),
                fingerprint=fingerprint,
                snapshot_at=fetched_at.isoformat(),
                snapshot_watermark=source_watermark,
                last_record_key=page_items[-1].news_id,
            )
            if has_more
            else None
        )
        return ProviderPage(
            items=page_items,
            next_cursor=next_cursor,
            source_watermark=source_watermark,
            fetched_at=fetched_at,
            complete=not has_more,
        )

    async def _fetch_snapshot(
        self, *, context_deadline: datetime
    ) -> tuple[list[dict[str, Any]], str, datetime, str]:
        try:
            return await self._fetch_api_snapshot(context_deadline=context_deadline)
        except (ProviderTimeoutError, ProviderUnavailableError):
            return await self._fetch_listing_snapshot(context_deadline=context_deadline)

    async def _fetch_api_snapshot(
        self, *, context_deadline: datetime
    ) -> tuple[list[dict[str, Any]], str, datetime, str]:
        records: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        offset = 0
        total: int | None = None
        source_url = ""
        fetched_at = self._clock().astimezone(UTC)
        while offset <= 100_000:
            payload, response, page_fetched_at = await self._fetch_page(
                offset=offset, context_deadline=context_deadline
            )
            if not source_url:
                source_url = str(response.url)
            fetched_at = page_fetched_at
            page_records, page_total = _hkma_records(payload)
            if total is None:
                total = page_total
            elif page_total is not None and page_total != total:
                raise ProviderCursorError(
                    "HKMA result size changed during pagination", code="SNAPSHOT_CHANGED"
                )
            for record in page_records:
                key = _raw_news_record_key(record)
                if key in seen_keys:
                    raise ProviderCursorError(
                        "HKMA returned a duplicate page", code="DUPLICATE_PAGE"
                    )
                seen_keys.add(key)
                records.append(record)
            if not page_records:
                if total not in {None, 0}:
                    raise ProviderSchemaError(
                        "HKMA returned an empty page before datasize was reached",
                        code="SCHEMA_DRIFT",
                    )
                break
            if total is not None and len(records) >= total:
                break
            if len(page_records) < _MAX_API_PAGE_SIZE and total is None:
                break
            offset += len(page_records)
        else:
            raise ProviderCursorError(
                "HKMA snapshot exceeds the bounded pagination limit", code="CURSOR_LIMIT"
            )
        source_watermark = canonical_json_checksum({"datasize": total, "records": records})
        return records, source_url, fetched_at, source_watermark

    async def _fetch_listing_snapshot(
        self, *, context_deadline: datetime
    ) -> tuple[list[dict[str, Any]], str, datetime, str]:
        remaining = (
            context_deadline.astimezone(UTC) - self._clock().astimezone(UTC)
        ).total_seconds()
        if remaining <= 0:
            raise ProviderTimeoutError("HKMA fallback deadline has elapsed", retryable=True)
        try:
            response = await self._client.get(
                HKMA_PRESS_RELEASES_LISTING_URL,
                timeout=min(self._timeout_seconds, remaining),
            )
        except httpx.TimeoutException as error:
            raise ProviderTimeoutError("HKMA fallback request timed out", retryable=True) from error
        except httpx.HTTPError as error:
            raise ProviderUnavailableError(
                "HKMA fallback request failed", retryable=True
            ) from error
        if response.status_code >= 500:
            raise ProviderUnavailableError("HKMA fallback is unavailable", retryable=True)
        if response.status_code >= 400:
            raise ProviderSchemaError(
                f"HKMA fallback returned HTTP {response.status_code}", code="PROVIDER_HTTP_ERROR"
            )
        parser = _HkmaPressReleaseListingParser()
        parser.feed(response.text)
        records = parser.records
        if not records:
            raise ProviderSchemaError(
                "HKMA fallback listing contains no press releases", code="SCHEMA_DRIFT"
            )
        fetched_at = self._clock().astimezone(UTC)
        source_url = str(response.url)
        source_watermark = canonical_json_checksum(
            {"source": "official_listing", "records": records}
        )
        return records, source_url, fetched_at, source_watermark

    async def _fetch_page(
        self, *, offset: int, context_deadline: datetime
    ) -> tuple[dict[str, Any], httpx.Response, datetime]:
        return await self._get_json(
            params={"lang": "en", "offset": offset, "pagesize": _MAX_API_PAGE_SIZE},
            context_deadline=context_deadline,
        )


class _HkmaPressReleaseListingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.records: list[dict[str, Any]] = []
        self._container_depth = 0
        self._in_list_item = False
        self._list_item_text: list[str] = []
        self._pending_date: str | None = None
        self._href: str | None = None
        self._title: str | None = None
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "div":
            if self._container_depth:
                self._container_depth += 1
            elif attributes.get("id") == "press-release-result":
                self._container_depth = 1
            return
        if not self._container_depth:
            return
        if tag == "li":
            self._in_list_item = True
            self._list_item_text = []
            self._href = None
            self._title = None
            self._anchor_text = []
        elif tag == "a" and self._in_list_item:
            href = attributes.get("href")
            if href is not None and href.startswith("/eng/news-and-media/press-releases/"):
                self._href = href
                self._title = attributes.get("title")

    def handle_data(self, data: str) -> None:
        if not self._container_depth or not self._in_list_item:
            return
        self._list_item_text.append(data)
        if self._href is not None:
            self._anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._container_depth:
            self._container_depth -= 1
            return
        if tag != "li" or not self._container_depth or not self._in_list_item:
            return
        text = " ".join("".join(self._list_item_text).split())
        if self._href is None:
            self._pending_date = _hkma_listing_date(text)
        elif self._pending_date is not None:
            title = (self._title or " ".join("".join(self._anchor_text).split())).strip()
            if title:
                self.records.append(
                    {
                        "title": title,
                        "link": urljoin(HKMA_PRESS_RELEASES_LISTING_URL, self._href),
                        "date": self._pending_date,
                    }
                )
            self._pending_date = None
        self._in_list_item = False


def _hkma_listing_date(value: str) -> str | None:
    try:
        return datetime.strptime(value, "%d %b %Y").date().isoformat()
    except ValueError:
        return None


def _news_event(
    record: Mapping[str, Any],
    *,
    fetched_at: datetime,
    source_url: str,
    provider_id: str,
    source_name: str,
) -> NewsEvent:
    title = _required_text(record, "title", "result.records.title")
    link = _required_text(record, "link", "result.records.link")
    published_date = _parse_date(_required_text(record, "date", "result.records.date"))
    canonical_url = canonicalize_url(link)
    provider_record_id = stable_provider_record_id("hkma-release", canonical_url)
    return NewsEvent(
        news_id=stable_provider_record_id(
            "news", provider_id, provider_record_id, canonical_url, published_date
        ),
        title=title,
        summary=None,
        body=None,
        content_mode=ContentMode.HEADLINE,
        language="en",
        source_name=source_name,
        source_tier=SourceTier.OFFICIAL,
        canonical_url=canonical_url,
        published_at=None,
        published_date=published_date,
        time_precision="date",
        first_seen_at=fetched_at,
        available_at=fetched_at,
        availability_basis=AvailabilityBasis.FIRST_SEEN,
        regions=[Region.HK],
        content_hash_sha256=canonical_json_checksum(
            {"title": title, "summary": None, "body": None}
        ),
        usage_rights=UsageRights(
            storage_allowed=True,
            internal_analysis_allowed=True,
            external_llm_allowed=True,
            embedding_allowed=True,
            redistribution_allowed=False,
        ),
        source=source_ref(
            provider_id=provider_id,
            provider_record_id=provider_record_id,
            source_name=source_name,
            source_url=canonical_url,
            retrieved_at=fetched_at,
            checksum_payload={"title": title, "link": canonical_url, "date": published_date},
        ),
    )


def _dataset_rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = payload.get("dataSet")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ProviderSchemaError("C&SD dataSet must be a list of objects", code="SCHEMA_DRIFT")
    return rows


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderSchemaError(f"{path} must be an object", code="SCHEMA_DRIFT")
    return value


def _required_text(row: Mapping[str, Any], key: str, path: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProviderSchemaError(f"{path} is missing", code="SCHEMA_DRIFT")
    return value.strip()


def _series_definition(row: Mapping[str, Any]) -> tuple[str, _CsdSeriesDefinition]:
    sv = _required_text(row, "sv", "dataSet.sv")
    definition = HK_CSD_SERIES_REGISTRY.get(sv)
    if definition is None:
        raise ProviderSchemaError(
            f"C&SD series {sv} is not in the approved registry", code="SERIES_UNRESOLVED"
        )
    description = _required_text(row, "svDesc", "dataSet.svDesc")
    frequency = _required_text(row, "freq", "dataSet.freq")
    if description != definition.source_description:
        raise ProviderSchemaError(
            f"C&SD series {sv} metadata does not match the approved registry",
            code="SERIES_METADATA_MISMATCH",
        )
    if frequency != definition.source_frequency:
        raise ProviderSchemaError(
            f"C&SD series {sv} frequency does not match the approved registry",
            code="SERIES_METADATA_MISMATCH",
        )
    return sv, definition


def _series_id(dataset_id: str, sv: str) -> str:
    return f"macro:HK:CENSTATD:{dataset_id}:{sv}"


def _ensure_requested_series_are_registered(series_ids: list[str], dataset_id: str) -> None:
    allowed_ids = {_series_id(dataset_id, sv) for sv in HK_CSD_SERIES_REGISTRY}
    unknown_ids = set(series_ids) - allowed_ids
    if unknown_ids:
        raise ProviderSchemaError(
            "requested C&SD series are not in the approved registry: "
            + ", ".join(sorted(unknown_ids)),
            code="SERIES_UNRESOLVED",
        )


def _news_overlaps_query_window(item: NewsEvent, query: NewsQuery) -> bool:
    if item.published_at is not None:
        return query.published_from <= item.published_at < query.published_to
    assert item.published_date is not None
    day_start = datetime.combine(item.published_date, time.min, UTC)
    day_end = day_start + timedelta(days=1)
    return day_start < query.published_to and day_end > query.published_from


def _news_sort_at(item: NewsEvent) -> datetime:
    if item.published_at is not None:
        return item.published_at
    assert item.published_date is not None
    return datetime.combine(item.published_date, time.min, UTC)


def _optional_decimal(value: object, path: str) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise ProviderSchemaError(f"{path} is not numeric", code="SCHEMA_DRIFT") from exc


def _period_bounds(value: str) -> tuple[date, date]:
    if len(value) == 6 and value.isdigit():
        year, month = int(value[:4]), int(value[4:])
        if not 1 <= month <= 12:
            raise ProviderSchemaError("C&SD period month is invalid", code="SCHEMA_DRIFT")
        return date(year, month, 1), date(year, month, monthrange(year, month)[1])
    raise ProviderSchemaError("C&SD period format is unsupported", code="SCHEMA_DRIFT")


def _server_finished_at(header: Mapping[str, Any], fallback: datetime) -> datetime:
    count = header.get("count")
    if isinstance(count, Mapping) and isinstance(count.get("finished"), str):
        try:
            return datetime.fromisoformat(count["finished"]).astimezone(UTC)
        except ValueError as exc:
            raise ProviderSchemaError(
                "C&SD finished timestamp is invalid", code="SCHEMA_DRIFT"
            ) from exc
    return fallback


def _raw_news_record_key(record: Mapping[str, Any]) -> str:
    return canonical_json_checksum(
        {
            "title": _required_text(record, "title", "result.records.title"),
            "link": _required_text(record, "link", "result.records.link"),
            "date": _required_text(record, "date", "result.records.date"),
        }
    )


def _hkma_records(payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
    header = _mapping(payload.get("header"), "header")
    if header.get("success") is not True or header.get("err_code") not in {"0000", 0}:
        raise ProviderSchemaError("HKMA response reports failure", code="UPSTREAM_SCHEMA_ERROR")
    result = _mapping(payload.get("result"), "result")
    raw_records = result.get("records")
    if not isinstance(raw_records, list):
        raise ProviderSchemaError("HKMA records must be a list", code="SCHEMA_DRIFT")
    if not all(isinstance(record, dict) for record in raw_records):
        raise ProviderSchemaError("HKMA records must contain only objects", code="SCHEMA_DRIFT")
    datasize = result.get("datasize")
    if datasize is not None and (not isinstance(datasize, int) or datasize < 0):
        raise ProviderSchemaError(
            "HKMA datasize must be a non-negative integer", code="SCHEMA_DRIFT"
        )
    return raw_records, datasize


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ProviderSchemaError("HKMA release date is invalid", code="SCHEMA_DRIFT") from exc


def _empty_page(clock: Callable[[], datetime]) -> ProviderPage[MacroSeries]:
    return ProviderPage(items=[], fetched_at=clock().astimezone(UTC), complete=True)


def _duration_seconds(seconds: float) -> timedelta:
    return timedelta(seconds=seconds)


def _utc_now() -> datetime:
    return datetime.now(UTC)


HkLiveMacroProvider = HkCsdProvider
HkmaLiveNewsProvider = HkmaPressReleaseProvider

__all__ = [
    "HK_CSD_ALLOWED_HOSTS",
    "HK_CSD_API_URL",
    "HK_CSD_DATASET_ALLOWLIST",
    "HK_CSD_PROVIDER_ID",
    "HKMA_ALLOWED_HOSTS",
    "HKMA_PRESS_RELEASES_URL",
    "HKMA_PRESS_RELEASE_PROVIDER_ID",
    "HkCsdProvider",
    "HkLiveMacroProvider",
    "HkmaLiveNewsProvider",
    "HkmaPressReleaseProvider",
]
