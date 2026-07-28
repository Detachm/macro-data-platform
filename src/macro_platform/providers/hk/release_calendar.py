"""Official Hong Kong C&SD regular statistical release calendar."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from io import BytesIO
from typing import ClassVar
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile
from zoneinfo import ZoneInfo

import httpx

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
)

HK_CENSTATD_CALENDAR_BASE_URL = "https://www.censtatd.gov.hk/FileManager/EN/Common"
HK_CENSTATD_CALENDAR_PROVIDER_ID = "hk.censtatd.release-calendar.v1"
HK_CENSTATD_CALENDAR_ROLE = "hk.calendar.primary"
HK_CENSTATD_CALENDAR_SERIES_ID = "macro:HK:CENSTATD:release_calendar"
HK_CENSTATD_ALLOWED_HOSTS = frozenset({"www.censtatd.gov.hk"})

_HK_TIMEZONE = ZoneInfo("Asia/Hong_Kong")
_EXPECTED_HEADER = (
    "Release Date",
    "Subject",
    "Sub-subject",
    "Series",
    "Title",
    "Footnote No",
    "Footnote Content",
)
_MAX_XLSX_BYTES = 2_000_000
_MAX_XLSX_ENTRIES = 100
_MAX_XLSX_UNCOMPRESSED_BYTES = 10_000_000
_MAX_ROWS = 5_000
_MAX_CELL_LENGTH = 2_000
_SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_CELL_REFERENCE = re.compile(r"^(?P<column>[A-Z]+)\d+$")


class HkCenstatdReleaseCalendarProvider(LiveHttpProvider):
    """Read the annual C&SD XLSX schedule with a fixed 16:30 HKT release time."""

    provider_id: ClassVar[str] = HK_CENSTATD_CALENDAR_PROVIDER_ID
    source_name: ClassVar[str] = "Hong Kong C&SD regular press release schedule"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = HK_CENSTATD_CALENDAR_BASE_URL,
        timeout_seconds: float = 30,
        clock: Callable[[], datetime] = utc_now,
        cursor_signing_secret: str | None = None,
    ) -> None:
        super().__init__(
            client=client,
            base_url=base_url,
            allowed_hosts=HK_CENSTATD_ALLOWED_HOSTS,
            timeout_seconds=timeout_seconds,
            clock=clock,
            cursor_signing_secret=cursor_signing_secret,
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            regions={Region.HK},
            datasets={Dataset.MACRO_RELEASES},
            max_page_size=1000,
            supports_point_in_time=False,
            supports_revisions=True,
            supports_full_text=False,
            external_llm_allowed=True,
        )

    async def healthcheck(self) -> ProviderHealth:
        checked_at = self._clock().astimezone(UTC)
        try:
            await self._get(
                path=_calendar_filename(checked_at.year),
                params=None,
                context_deadline=checked_at + timedelta(seconds=self._timeout_seconds),
            )
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
        if Region.HK not in query.regions:
            return ProviderPage(items=[], fetched_at=self._clock().astimezone(UTC), complete=True)
        fingerprint = self._cursor_fingerprint(query, context)
        offset, previous_record_key, snapshot_at, snapshot_watermark = self._decode_cursor(
            query.cursor, fingerprint
        )
        documents: list[tuple[bytes, str, int]] = []
        for year in _query_years(query):
            response = await self._get(
                path=_calendar_filename(year),
                params=None,
                context_deadline=context.deadline_at,
            )
            documents.append((response.content, str(response.url), year))
        fetched_at = self._clock().astimezone(UTC)
        if query.as_of < fetched_at:
            raise UnsupportedCapabilityError(
                "C&SD release calendar does not provide historical point-in-time snapshots"
            )
        source_watermark = canonical_json_checksum(
            [
                (year, url, canonical_json_checksum(content.hex()))
                for content, url, year in documents
            ]
        )
        assert_cursor_snapshot(snapshot_watermark, source_watermark)
        assert_cursor_snapshot_at(snapshot_at, fetched_at)
        releases = [
            release
            for content, url, _ in documents
            for release in parse_censtatd_release_calendar(
                content,
                fetched_at=fetched_at,
                source_url=url,
                provider_id=self.provider_id,
                source_name=self.source_name,
            )
            if _release_overlaps_query_window(release, query)
            and release.available_at <= query.as_of
        ]
        releases = list({release.release_id: release for release in releases}.values())
        releases.sort(key=lambda item: (item.scheduled_at, item.release_id))
        if offset > 0 and (
            offset > len(releases) or releases[offset - 1].release_id != previous_record_key
        ):
            raise ProviderCursorError(
                "C&SD cursor predecessor does not match the source snapshot",
                code="SNAPSHOT_CHANGED",
            )
        if offset > len(releases):
            raise ProviderCursorError("C&SD cursor is past the result set", code="INVALID_CURSOR")
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


def parse_censtatd_release_calendar(
    content: bytes,
    *,
    fetched_at: datetime,
    source_url: str,
    provider_id: str,
    source_name: str,
) -> list[MacroRelease]:
    rows = _read_xlsx_rows(content)
    if not rows or tuple(rows[0].get(column, "") for column in "ABCDEFG") != _EXPECTED_HEADER:
        raise ProviderSchemaError("C&SD calendar XLSX header changed", code="SCHEMA_DRIFT")
    releases: list[MacroRelease] = []
    seen: set[str] = set()
    for row in rows[1:]:
        raw_date = row.get("A", "").strip()
        series = row.get("D", "").strip()
        title = row.get("E", "").strip()
        if not raw_date and not series and not title:
            continue
        if not raw_date or not series or not title:
            raise ProviderSchemaError(
                "C&SD calendar contains an incomplete row", code="SCHEMA_DRIFT"
            )
        release_date = _excel_date(raw_date)
        release_id = stable_provider_record_id("rel", HK_CENSTATD_CALENDAR_SERIES_ID, title)
        if release_id in seen:
            raise ProviderSchemaError(
                "C&SD calendar contains a duplicate release title", code="SCHEMA_DRIFT"
            )
        seen.add(release_id)
        scheduled_at = datetime.combine(release_date, time(16, 30), _HK_TIMEZONE).astimezone(UTC)
        releases.append(
            MacroRelease(
                release_id=release_id,
                series_id=HK_CENSTATD_CALENDAR_SERIES_ID,
                region=Region.HK,
                release_name=title,
                scheduled_at=scheduled_at,
                scheduled_date=None,
                time_precision="instant",
                released_at=None,
                available_at=fetched_at,
                period_start=release_date,
                period_end=release_date,
                actual=None,
                consensus=None,
                previous=None,
                unit="unknown",
                status="scheduled",
                source=source_ref(
                    provider_id=provider_id,
                    provider_record_id=stable_provider_record_id("hk-censtatd-release", title),
                    source_name=source_name,
                    source_url=source_url,
                    retrieved_at=fetched_at,
                    checksum_payload={
                        "release_date": release_date,
                        "subject": row.get("B", ""),
                        "sub_subject": row.get("C", ""),
                        "series": series,
                        "title": title,
                        "footnote_number": row.get("F", ""),
                        "footnote_content": row.get("G", ""),
                    },
                ),
            )
        )
    if not releases:
        raise ProviderSchemaError("C&SD calendar contains no releases", code="SCHEMA_DRIFT")
    return releases


def _read_xlsx_rows(content: bytes) -> list[dict[str, str]]:
    if not content or len(content) > _MAX_XLSX_BYTES:
        raise ProviderSchemaError("C&SD calendar XLSX size is invalid", code="SCHEMA_DRIFT")
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if (
                len(entries) > _MAX_XLSX_ENTRIES
                or sum(entry.file_size for entry in entries) > _MAX_XLSX_UNCOMPRESSED_BYTES
            ):
                raise ProviderSchemaError(
                    "C&SD calendar XLSX archive exceeds safety limits", code="SCHEMA_DRIFT"
                )
            shared_strings = _shared_strings(archive)
            sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    except (BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise ProviderSchemaError("C&SD calendar XLSX is malformed", code="SCHEMA_DRIFT") from exc
    rows: list[dict[str, str]] = []
    namespace = {"s": _SPREADSHEET_NS}
    for row_element in sheet.findall(".//s:row", namespace):
        if len(rows) >= _MAX_ROWS:
            raise ProviderSchemaError("C&SD calendar has too many rows", code="SCHEMA_DRIFT")
        row: dict[str, str] = {}
        for cell in row_element.findall("s:c", namespace):
            reference = cell.get("r", "")
            match = _CELL_REFERENCE.match(reference)
            if match is None:
                raise ProviderSchemaError(
                    "C&SD calendar contains an invalid cell reference", code="SCHEMA_DRIFT"
                )
            column = match.group("column")
            if column not in "ABCDEFG":
                continue
            value_element = cell.find("s:v", namespace)
            value = "" if value_element is None else value_element.text or ""
            if cell.get("t") == "s" and value:
                try:
                    value = shared_strings[int(value)]
                except (ValueError, IndexError) as exc:
                    raise ProviderSchemaError(
                        "C&SD calendar has an invalid shared string", code="SCHEMA_DRIFT"
                    ) from exc
            if len(value) > _MAX_CELL_LENGTH:
                raise ProviderSchemaError("C&SD calendar cell is too long", code="SCHEMA_DRIFT")
            row[column] = " ".join(value.split())
        rows.append(row)
    return rows


def _shared_strings(archive: ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    namespace = {"s": _SPREADSHEET_NS}
    return ["".join(item.itertext()) for item in root.findall("s:si", namespace)]


def _excel_date(value: str) -> date:
    try:
        serial = float(value)
    except ValueError as exc:
        raise ProviderSchemaError("C&SD calendar date is not numeric", code="SCHEMA_DRIFT") from exc
    if not serial.is_integer() or serial < 1 or serial > 100_000:
        raise ProviderSchemaError("C&SD calendar date is invalid", code="SCHEMA_DRIFT")
    return date(1899, 12, 30) + timedelta(days=int(serial))


def _query_years(query: MacroReleaseQuery) -> tuple[int, ...]:
    end_year = (query.scheduled_to - timedelta(microseconds=1)).year
    return tuple(range(query.scheduled_from.year, end_year + 1))


def _calendar_filename(year: int) -> str:
    return f"Regular_Press_Releases_Schedule_{year}.xlsx"


def _release_overlaps_query_window(item: MacroRelease, query: MacroReleaseQuery) -> bool:
    assert item.scheduled_at is not None
    return query.scheduled_from <= item.scheduled_at < query.scheduled_to


__all__ = [
    "HK_CENSTATD_CALENDAR_BASE_URL",
    "HK_CENSTATD_CALENDAR_PROVIDER_ID",
    "HK_CENSTATD_CALENDAR_ROLE",
    "HkCenstatdReleaseCalendarProvider",
    "parse_censtatd_release_calendar",
]
