from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from html.parser import HTMLParser
from typing import ClassVar
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
from macro_platform.normalization.common import canonical_json_checksum
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

CN_NBS_RELEASE_CALENDAR_URL = "https://www.stats.gov.cn/sj/fbrc/bnxxfb/"
CN_NBS_ALLOWED_HOSTS = frozenset({"www.stats.gov.cn"})
CN_NBS_PROVIDER_ID = "cn.nbs.release-calendar.v1"
CN_NBS_SERIES_ID = "macro:CN:NBS:release_calendar"
_CN_TIMEZONE = ZoneInfo("Asia/Shanghai")
_MONTH_PATTERN = re.compile(r"^(?P<day>\d{1,2})\s*/(?:[^\d]*)?(?P<time>\d{1,2}:\d{2})?$")
_YEAR_PATTERN = re.compile(r"(?P<year>20\d{2})年国家统计局")


class CnNbsReleaseProvider(LiveHttpProvider):
    provider_id: ClassVar[str] = CN_NBS_PROVIDER_ID
    source_name: ClassVar[str] = "National Bureau of Statistics of China release calendar"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = CN_NBS_RELEASE_CALENDAR_URL,
        timeout_seconds: float = 30,
        clock: Callable[[], datetime] | None = None,
        cursor_signing_secret: str | None = None,
    ) -> None:
        super().__init__(
            client=client,
            base_url=base_url,
            allowed_hosts=CN_NBS_ALLOWED_HOSTS,
            timeout_seconds=timeout_seconds,
            clock=clock or _utc_now,
            cursor_signing_secret=cursor_signing_secret,
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            regions={Region.CN},
            datasets={Dataset.MACRO_RELEASES},
            max_page_size=1000,
            supports_point_in_time=False,
            supports_revisions=False,
            supports_full_text=False,
            external_llm_allowed=True,
        )

    async def healthcheck(self) -> ProviderHealth:
        checked_at = self._clock().astimezone(UTC)
        try:
            await self._get_text(
                context_deadline=checked_at.replace(microsecond=0)
                + _duration_seconds(self._timeout_seconds)
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

    async def fetch_macro_releases(
        self, query: MacroReleaseQuery, context: FetchContext
    ) -> ProviderPage[MacroRelease]:
        if Region.CN not in query.regions:
            return ProviderPage(
                items=[],
                fetched_at=self._clock().astimezone(UTC),
                complete=True,
            )
        fingerprint = self._cursor_fingerprint(query, context)
        offset, last_record_key, snapshot_at, snapshot_watermark = self._decode_cursor(
            query.cursor, fingerprint
        )
        html, response, fetched_at = await self._get_text(context_deadline=context.deadline_at)
        source_watermark = canonical_json_checksum(html)
        assert_cursor_snapshot(snapshot_watermark, source_watermark)
        assert_cursor_snapshot_at(snapshot_at, fetched_at)
        if query.as_of < fetched_at:
            raise UnsupportedCapabilityError(
                "NBS release calendar does not provide historical point-in-time snapshots"
            )
        year_match = _YEAR_PATTERN.search(html)
        if year_match is None:
            raise ProviderSchemaError(
                "NBS release calendar year heading is missing", code="SCHEMA_DRIFT"
            )
        releases = parse_nbs_release_calendar(
            html,
            year=int(year_match.group("year")),
            fetched_at=fetched_at,
            source_url=str(response.url),
            provider_id=self.provider_id,
            source_name=self.source_name,
        )
        all_items = [
            item
            for item in releases
            if _release_overlaps_query_window(item, query) and item.available_at <= query.as_of
        ]
        all_items.sort(key=lambda item: (_release_sort_at(item), item.release_id))
        if offset > 0 and (
            offset > len(all_items) or all_items[offset - 1].release_id != last_record_key
        ):
            raise ProviderCursorError(
                "NBS cursor predecessor does not match the source snapshot",
                code="SNAPSHOT_CHANGED",
            )
        if offset > len(all_items):
            raise ProviderCursorError("NBS cursor is past the result set", code="INVALID_CURSOR")
        items = all_items[offset : offset + query.limit]
        has_more = offset + len(items) < len(all_items)
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


def parse_nbs_release_calendar(
    html: str,
    *,
    year: int,
    fetched_at: datetime,
    source_url: str,
    provider_id: str,
    source_name: str,
) -> list[MacroRelease]:
    rows = _TableParser.parse(html)
    header_index, month_columns = _find_schedule_header(rows)
    parsed: list[MacroRelease] = []
    seen: set[tuple[str, date]] = set()
    for row_index in range(header_index + 1, len(rows)):
        row = rows[row_index]
        if len(row) <= 1:
            continue
        name = row[1].strip() if len(row) > 1 else ""
        if not name or name.isdigit() or name in {"内容", "合计"}:
            continue
        for month, column in month_columns.items():
            if column >= len(row):
                continue
            match = _MONTH_PATTERN.match(_row_month_value(rows, row_index, row, column).strip())
            if match is None:
                continue
            day = int(match.group("day"))
            if day < 1 or day > 31:
                raise ProviderSchemaError(
                    "NBS schedule contains an invalid day", code="SCHEMA_DRIFT"
                )
            release_date = date(year, month, day)
            key = (name, release_date)
            if key in seen:
                continue
            seen.add(key)
            release_time = _parse_time(match.group("time"))
            time_precision = "instant" if match.group("time") else "date"
            scheduled_at = (
                datetime.combine(release_date, release_time, _CN_TIMEZONE).astimezone(UTC)
                if match.group("time")
                else None
            )
            record_id = stable_provider_record_id("cn-nbs-release", year, month, day, name)
            parsed.append(
                MacroRelease(
                    release_id=stable_provider_record_id(
                        "rel",
                        CN_NBS_SERIES_ID,
                        scheduled_at.isoformat() if scheduled_at is not None else release_date,
                        name,
                    ),
                    series_id=CN_NBS_SERIES_ID,
                    region=Region.CN,
                    release_name=name,
                    scheduled_at=scheduled_at,
                    scheduled_date=release_date if time_precision == "date" else None,
                    time_precision=time_precision,
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
                        provider_record_id=record_id,
                        source_name=source_name,
                        source_url=source_url,
                        retrieved_at=fetched_at,
                        checksum_payload={
                            "year": year,
                            "month": month,
                            "day": day,
                            "release_name": name,
                        },
                    ),
                )
            )
    if not parsed:
        raise ProviderSchemaError(
            "NBS release calendar has no parseable schedule rows", code="SCHEMA_DRIFT"
        )
    return parsed


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    @classmethod
    def parse(cls, html: str) -> list[list[str]]:
        parser = cls()
        parser.feed(html)
        parser.close()
        return parser.rows

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def _find_schedule_header(rows: list[list[str]]) -> tuple[int, dict[int, int]]:
    for index, row in enumerate(rows):
        if "内容" not in row:
            continue
        month_columns = {
            month: column
            for column, value in enumerate(row)
            for month in range(1, 13)
            if value.strip() == f"{month}月"
        }
        if month_columns:
            return index, month_columns
    raise ProviderSchemaError(
        "NBS release calendar header is missing month columns", code="SCHEMA_DRIFT"
    )


def _row_month_value(rows: list[list[str]], row_index: int, row: list[str], column: int) -> str:
    value = row[column]
    if _MONTH_PATTERN.match(value.strip()) is None or ":" in value:
        return value
    if row_index + 1 >= len(rows):
        return value
    continuation = rows[row_index + 1]
    continuation_index = column
    if len(continuation) <= continuation_index and column >= 2:
        continuation_index = column - 2
    if continuation_index >= len(continuation):
        return value
    time_match = re.search(r"\b\d{1,2}:\d{2}\b", continuation[continuation_index])
    if time_match is None:
        return value
    return f"{value} {time_match.group(0)}"


def _parse_time(value: str | None) -> time:
    if value is None:
        return time.min
    hour, minute = (int(part) for part in value.split(":"))
    if hour > 23 or minute > 59:
        raise ProviderSchemaError("NBS schedule contains an invalid time", code="SCHEMA_DRIFT")
    return time(hour, minute)


def _release_overlaps_query_window(item: MacroRelease, query: MacroReleaseQuery) -> bool:
    if item.scheduled_at is not None:
        return query.scheduled_from <= item.scheduled_at < query.scheduled_to
    assert item.scheduled_date is not None
    day_start = datetime.combine(item.scheduled_date, time.min, UTC)
    day_end = day_start + timedelta(days=1)
    return day_start < query.scheduled_to and day_end > query.scheduled_from


def _release_sort_at(item: MacroRelease) -> datetime:
    if item.scheduled_at is not None:
        return item.scheduled_at
    assert item.scheduled_date is not None
    return datetime.combine(item.scheduled_date, time.min, UTC)


def _duration_seconds(seconds: float) -> timedelta:
    return timedelta(seconds=seconds)


def _utc_now() -> datetime:
    return datetime.now(UTC)


CnLiveMacroProvider = CnNbsReleaseProvider

__all__ = [
    "CN_NBS_ALLOWED_HOSTS",
    "CN_NBS_PROVIDER_ID",
    "CN_NBS_RELEASE_CALENDAR_URL",
    "CnLiveMacroProvider",
    "CnNbsReleaseProvider",
    "parse_nbs_release_calendar",
]
