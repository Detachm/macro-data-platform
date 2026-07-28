"""Composite official US macro release calendar from OMB/BLS and BEA."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from html.parser import HTMLParser
from io import BytesIO
from typing import ClassVar
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from pypdf import PdfReader

from macro_platform.contracts.common import Region
from macro_platform.contracts.macro import MacroRelease, MacroReleaseQuery
from macro_platform.contracts.provider import (
    Dataset,
    FetchContext,
    ProviderCapabilities,
    ProviderHealth,
    ProviderPage,
)
from macro_platform.normalization.common import canonical_json_checksum, utc_now
from macro_platform.providers.base import (
    ProviderCursorError,
    ProviderSchemaError,
    UnsupportedCapabilityError,
)
from macro_platform.providers.live import (
    LiveHttpProvider,
    assert_cursor_snapshot,
    assert_cursor_snapshot_at,
    health_status_for_error,
    source_ref,
    stable_provider_record_id,
    validate_allowlisted_url,
)

US_OFFICIAL_CALENDAR_PROVIDER_ID = "us.official.release-calendar.v1"
US_OFFICIAL_CALENDAR_ROLE = "us.calendar.primary"
US_OFFICIAL_CALENDAR_SERIES_ID = "macro:US:OFFICIAL:release_calendar"
US_OMB_BASE_URL = "https://www.whitehouse.gov"
US_OMB_CALENDAR_PATH = (
    "/omb/information-resources/guidance/us-principal-federal-economic-indicators/"
)
US_BEA_CALENDAR_URL = "https://www.bea.gov/news/schedule"
US_OMB_ALLOWED_HOSTS = frozenset({"www.whitehouse.gov"})
US_BEA_ALLOWED_HOSTS = frozenset({"www.bea.gov"})

_NEW_YORK = ZoneInfo("America/New_York")
_MAX_PDF_BYTES = 2_000_000
_MAX_PDF_PAGES = 30
_MAX_PDF_TEXT_CHARS = 500_000
_OMB_FILENAME = re.compile(r"pfei_schedule_release_dates_cy(?P<year>20\d{2})\.pdf", re.I)
_DAY_ROW = re.compile(r"^(?:\d{1,2}\s+){11}\d{1,2}$")
_BEA_YEAR = re.compile(r"\bYear\s+(?P<year>20\d{2})\b", re.I)
_BEA_DATE_TIME = re.compile(
    r"^(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})\s+(?P<clock>\d{1,2}:\d{2}\s*[AP]M)$",
    re.I,
)
_BLS_INDICATORS = (
    "The Employment Situation",
    "Producer Price Indexes",
    "Consumer Price Index",
)


class UsOfficialReleaseCalendarProvider(LiveHttpProvider):
    """Combine the official OMB PFEI schedule with BEA's current schedule."""

    provider_id: ClassVar[str] = US_OFFICIAL_CALENDAR_PROVIDER_ID
    source_name: ClassVar[str] = "US official macro release calendars"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        omb_base_url: str = US_OMB_BASE_URL,
        bea_url: str = US_BEA_CALENDAR_URL,
        timeout_seconds: float = 30,
        clock: Callable[[], datetime] = utc_now,
        cursor_signing_secret: str | None = None,
    ) -> None:
        super().__init__(
            client=client,
            base_url=omb_base_url,
            allowed_hosts=US_OMB_ALLOWED_HOSTS,
            timeout_seconds=timeout_seconds,
            clock=clock,
            cursor_signing_secret=cursor_signing_secret,
        )
        validate_allowlisted_url(bea_url, US_BEA_ALLOWED_HOSTS)
        parsed_bea_url = urlsplit(bea_url)
        self._bea = LiveHttpProvider(
            client=self._client,
            base_url=f"{parsed_bea_url.scheme}://{parsed_bea_url.netloc}",
            allowed_hosts=US_BEA_ALLOWED_HOSTS,
            timeout_seconds=timeout_seconds,
            clock=clock,
            cursor_signing_secret=cursor_signing_secret,
        )
        self._bea_path = parsed_bea_url.path

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            regions={Region.US},
            datasets={Dataset.MACRO_RELEASES},
            max_page_size=1000,
            supports_point_in_time=False,
            supports_revisions=True,
            supports_full_text=False,
            external_llm_allowed=True,
        )

    async def healthcheck(self) -> ProviderHealth:
        checked_at = self._clock().astimezone(UTC)
        deadline = checked_at + timedelta(seconds=self._timeout_seconds)
        try:
            await self._get_text(path=US_OMB_CALENDAR_PATH, context_deadline=deadline)
            await self._bea._get_text(path=self._bea_path, context_deadline=deadline)
        except Exception as exc:  # noqa: BLE001 - health must remain non-fatal
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

    async def fetch_macro_releases(
        self, query: MacroReleaseQuery, context: FetchContext
    ) -> ProviderPage[MacroRelease]:
        if Region.US not in query.regions:
            return ProviderPage(items=[], fetched_at=self._clock().astimezone(UTC), complete=True)
        fingerprint = self._cursor_fingerprint(query, context)
        offset, previous_record_key, snapshot_at, snapshot_watermark = self._decode_cursor(
            query.cursor, fingerprint
        )
        landing_html, landing_response, _ = await self._get_text(
            path=US_OMB_CALENDAR_PATH,
            context_deadline=context.deadline_at,
        )
        calendar_urls = _omb_calendar_urls(landing_html, str(landing_response.url))
        omb_documents: list[tuple[bytes, str, int]] = []
        for year in _query_years(query):
            try:
                url = calendar_urls[year]
            except KeyError as exc:
                raise ProviderSchemaError(
                    f"OMB PFEI landing page has no calendar for {year}", code="SCHEMA_DRIFT"
                ) from exc
            validate_allowlisted_url(url, US_OMB_ALLOWED_HOSTS)
            parsed = urlsplit(url)
            response = await self._get(
                path=parsed.path,
                params=None,
                context_deadline=context.deadline_at,
            )
            omb_documents.append((response.content, str(response.url), year))
        bea_html, bea_response, _ = await self._bea._get_text(
            path=self._bea_path, context_deadline=context.deadline_at
        )
        bea_documents = await self._bea_documents(
            query=query,
            landing_html=bea_html,
            landing_url=str(bea_response.url),
            context=context,
        )
        fetched_at = self._clock().astimezone(UTC)
        if query.as_of < fetched_at:
            raise UnsupportedCapabilityError(
                "US official calendars do not provide historical point-in-time snapshots"
            )
        source_watermark = canonical_json_checksum(
            {
                "omb": [
                    (year, url, canonical_json_checksum(content.hex()))
                    for content, url, year in omb_documents
                ],
                "bea": [
                    (year, url, canonical_json_checksum(html)) for html, url, year in bea_documents
                ],
            }
        )
        assert_cursor_snapshot(snapshot_watermark, source_watermark)
        assert_cursor_snapshot_at(snapshot_at, fetched_at)
        releases = [
            release
            for content, url, year in omb_documents
            for release in parse_omb_bls_release_calendar(
                content,
                year=year,
                fetched_at=fetched_at,
                source_url=url,
                provider_id=self.provider_id,
            )
        ]
        releases.extend(
            release
            for html, url, _ in bea_documents
            for release in parse_bea_release_calendar(
                html,
                fetched_at=fetched_at,
                source_url=url,
                provider_id=self.provider_id,
            )
        )
        releases = [
            release
            for release in releases
            if _release_overlaps_query_window(release, query)
            and release.available_at <= query.as_of
        ]
        releases = list({release.release_id: release for release in releases}.values())
        releases.sort(key=lambda item: (_release_sort_at(item), item.release_id))
        if offset > 0 and (
            offset > len(releases) or releases[offset - 1].release_id != previous_record_key
        ):
            raise ProviderCursorError(
                "US calendar cursor predecessor does not match the source snapshot",
                code="SNAPSHOT_CHANGED",
            )
        if offset > len(releases):
            raise ProviderCursorError(
                "US calendar cursor is past the result set", code="INVALID_CURSOR"
            )
        items = releases[offset : offset + query.limit]
        has_more = offset + len(items) < len(releases)
        next_cursor = (
            self._encode_cursor(
                offset=offset + len(items),
                fingerprint=fingerprint,
                snapshot_at=fetched_at.isoformat(),
                snapshot_watermark=source_watermark,
                last_record_key=items[-1].release_id if items else None,
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

    async def _bea_documents(
        self,
        *,
        query: MacroReleaseQuery,
        landing_html: str,
        landing_url: str,
        context: FetchContext,
    ) -> list[tuple[str, str, int]]:
        landing_year = _bea_schedule_year(landing_html)
        documents = {landing_year: (landing_html, landing_url, landing_year)}
        calendar_urls = _bea_calendar_urls(landing_html, landing_url)
        for year in _query_years(query):
            if year in documents:
                continue
            try:
                url = calendar_urls[year]
            except KeyError as exc:
                raise ProviderSchemaError(
                    f"BEA landing page has no calendar for {year}", code="SCHEMA_DRIFT"
                ) from exc
            validate_allowlisted_url(url, US_BEA_ALLOWED_HOSTS)
            response = await self._bea._get(
                path=urlsplit(url).path,
                params=None,
                context_deadline=context.deadline_at,
            )
            html = response.text
            if _bea_schedule_year(html) != year:
                raise ProviderSchemaError(
                    "BEA calendar link resolved to the wrong year", code="SCHEMA_DRIFT"
                )
            documents[year] = (html, str(response.url), year)
        return [documents[year] for year in _query_years(query)]


def parse_omb_bls_release_calendar(
    content: bytes,
    *,
    year: int,
    fetched_at: datetime,
    source_url: str,
    provider_id: str,
) -> list[MacroRelease]:
    if not content or len(content) > _MAX_PDF_BYTES:
        raise ProviderSchemaError("OMB PFEI PDF size is invalid", code="SCHEMA_DRIFT")
    try:
        reader = PdfReader(BytesIO(content), strict=True)
        if len(reader.pages) > _MAX_PDF_PAGES:
            raise ProviderSchemaError("OMB PFEI PDF has too many pages", code="SCHEMA_DRIFT")
        text_content = "\n".join(page.extract_text() or "" for page in reader.pages)
        if len(text_content) > _MAX_PDF_TEXT_CHARS:
            raise ProviderSchemaError("OMB PFEI PDF text is too large", code="SCHEMA_DRIFT")
    except ProviderSchemaError:
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf exposes several parser-specific errors
        raise ProviderSchemaError("OMB PFEI PDF is malformed", code="SCHEMA_DRIFT") from exc
    lines = [" ".join(line.split()) for line in text_content.splitlines() if line.strip()]
    releases: list[MacroRelease] = []
    for indicator in _BLS_INDICATORS:
        try:
            index = lines.index(indicator)
        except ValueError as exc:
            raise ProviderSchemaError(
                f"OMB PFEI PDF is missing {indicator}", code="SCHEMA_DRIFT"
            ) from exc
        day_line = next(
            (line for line in lines[index + 1 : index + 5] if _DAY_ROW.match(line)), None
        )
        if day_line is None:
            raise ProviderSchemaError(
                f"OMB PFEI PDF has no 12-month row for {indicator}", code="SCHEMA_DRIFT"
            )
        days = [int(value) for value in day_line.split()]
        for month, day in enumerate(days, start=1):
            try:
                scheduled_date = date(year, month, day)
            except ValueError as exc:
                raise ProviderSchemaError(
                    f"OMB PFEI PDF has an invalid date for {indicator}", code="SCHEMA_DRIFT"
                ) from exc
            period_end = scheduled_date.replace(day=1) - timedelta(days=1)
            period_start = period_end.replace(day=1)
            release_name = f"{indicator} — {period_start:%B %Y}"
            release_id = stable_provider_record_id(
                "rel", US_OFFICIAL_CALENDAR_SERIES_ID, "BLS", indicator, period_start
            )
            releases.append(
                MacroRelease(
                    release_id=release_id,
                    series_id=US_OFFICIAL_CALENDAR_SERIES_ID,
                    region=Region.US,
                    release_name=release_name,
                    scheduled_at=None,
                    scheduled_date=scheduled_date,
                    time_precision="date",
                    released_at=None,
                    available_at=fetched_at,
                    period_start=period_start,
                    period_end=period_end,
                    actual=None,
                    consensus=None,
                    previous=None,
                    unit="unknown",
                    status="scheduled",
                    source=source_ref(
                        provider_id=provider_id,
                        provider_record_id=stable_provider_record_id(
                            "us-bls-release", indicator, period_start
                        ),
                        source_name="OMB Principal Federal Economic Indicators schedule",
                        source_url=source_url,
                        retrieved_at=fetched_at,
                        checksum_payload={
                            "indicator": indicator,
                            "reference_period": period_start,
                            "scheduled_date": scheduled_date,
                        },
                    ),
                )
            )
    return releases


def parse_bea_release_calendar(
    html: str,
    *,
    fetched_at: datetime,
    source_url: str,
    provider_id: str,
) -> list[MacroRelease]:
    parser = _BeaScheduleParser()
    parser.feed(html)
    parser.close()
    year = _bea_year_from_page_text(parser.page_text)
    releases: list[MacroRelease] = []
    for row in parser.rows:
        match = _BEA_DATE_TIME.match(row.schedule)
        if match is None:
            continue
        try:
            local_at = datetime.strptime(
                f"{year} {match.group('month')} {match.group('day')} {match.group('clock')}",
                "%Y %B %d %I:%M %p",
            ).replace(tzinfo=_NEW_YORK)
        except ValueError as exc:
            raise ProviderSchemaError(
                "BEA schedule contains an invalid date", code="SCHEMA_DRIFT"
            ) from exc
        scheduled_at = local_at.astimezone(UTC)
        release_id = stable_provider_record_id(
            "rel", US_OFFICIAL_CALENDAR_SERIES_ID, "BEA", row.title
        )
        releases.append(
            MacroRelease(
                release_id=release_id,
                series_id=US_OFFICIAL_CALENDAR_SERIES_ID,
                region=Region.US,
                release_name=row.title,
                scheduled_at=scheduled_at,
                scheduled_date=None,
                time_precision="instant",
                released_at=None,
                available_at=fetched_at,
                period_start=scheduled_at.date(),
                period_end=scheduled_at.date(),
                actual=None,
                consensus=None,
                previous=None,
                unit="unknown",
                status="scheduled",
                source=source_ref(
                    provider_id=provider_id,
                    provider_record_id=stable_provider_record_id("us-bea-release", row.title),
                    source_name="US Bureau of Economic Analysis release schedule",
                    source_url=source_url,
                    retrieved_at=fetched_at,
                    checksum_payload={"title": row.title, "scheduled_at": scheduled_at},
                ),
            )
        )
    if not releases:
        raise ProviderSchemaError("BEA schedule has no dated releases", code="SCHEMA_DRIFT")
    return releases


@dataclass(frozen=True)
class _BeaRow:
    schedule: str
    title: str


class _BeaScheduleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[_BeaRow] = []
        self._page_parts: list[str] = []
        self._in_row = False
        self._field: str | None = None
        self._schedule_parts: list[str] = []
        self._title_parts: list[str] = []

    @property
    def page_text(self) -> str:
        return " ".join(" ".join(self._page_parts).split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = set((dict(attrs).get("class") or "").split())
        if tag == "tr":
            self._in_row = True
            self._schedule_parts = []
            self._title_parts = []
        elif tag == "td" and self._in_row:
            if "scheduled-date" in classes:
                self._field = "schedule"
            elif "release-title" in classes:
                self._field = "title"

    def handle_data(self, data: str) -> None:
        self._page_parts.append(data)
        if self._field == "schedule":
            self._schedule_parts.append(data)
        elif self._field == "title":
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td":
            self._field = None
        elif tag == "tr" and self._in_row:
            schedule = " ".join(" ".join(self._schedule_parts).split())
            title = " ".join(" ".join(self._title_parts).split())
            if schedule and title:
                self.rows.append(_BeaRow(schedule=schedule, title=title))
            self._in_row = False
            self._field = None


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(" ".join(self._text_parts).split())))
            self._href = None
            self._text_parts = []


def _omb_calendar_urls(html: str, source_url: str) -> dict[int, str]:
    from urllib.parse import urljoin

    parser = _LinkParser()
    parser.feed(html)
    parser.close()
    calendars: dict[int, str] = {}
    for href, _ in parser.links:
        match = _OMB_FILENAME.search(href)
        if match is not None:
            calendars[int(match.group("year"))] = urljoin(source_url, href)
    if not calendars:
        raise ProviderSchemaError("OMB PFEI landing page has no PDF links", code="SCHEMA_DRIFT")
    return calendars


def _bea_calendar_urls(html: str, source_url: str) -> dict[int, str]:
    from urllib.parse import urljoin

    parser = _LinkParser()
    parser.feed(html)
    parser.close()
    calendars: dict[int, str] = {}
    for href, label in parser.links:
        match = re.fullmatch(r"(?P<year>20\d{2}) Schedule", label)
        if match is not None:
            calendars[int(match.group("year"))] = urljoin(source_url, href)
    return calendars


def _bea_schedule_year(html: str) -> int:
    parser = _BeaScheduleParser()
    parser.feed(html)
    parser.close()
    return _bea_year_from_page_text(parser.page_text)


def _bea_year_from_page_text(page_text: str) -> int:
    match = _BEA_YEAR.search(page_text)
    if match is None:
        raise ProviderSchemaError("BEA schedule year is missing", code="SCHEMA_DRIFT")
    return int(match.group("year"))


def _query_years(query: MacroReleaseQuery) -> tuple[int, ...]:
    end_year = (query.scheduled_to - timedelta(microseconds=1)).year
    return tuple(range(query.scheduled_from.year, end_year + 1))


def _release_overlaps_query_window(item: MacroRelease, query: MacroReleaseQuery) -> bool:
    if item.scheduled_at is not None:
        return query.scheduled_from <= item.scheduled_at < query.scheduled_to
    assert item.scheduled_date is not None
    day_start = datetime.combine(item.scheduled_date, time.min, UTC)
    return day_start < query.scheduled_to and day_start + timedelta(days=1) > query.scheduled_from


def _release_sort_at(item: MacroRelease) -> datetime:
    if item.scheduled_at is not None:
        return item.scheduled_at
    assert item.scheduled_date is not None
    return datetime.combine(item.scheduled_date, time.min, UTC)


__all__ = [
    "US_OFFICIAL_CALENDAR_PROVIDER_ID",
    "US_OFFICIAL_CALENDAR_ROLE",
    "UsOfficialReleaseCalendarProvider",
    "parse_bea_release_calendar",
    "parse_omb_bls_release_calendar",
]
