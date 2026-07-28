"""Deterministic report-day policy derived from reviewed exchange calendars."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from importlib.metadata import version
from typing import Literal, Protocol, cast

import exchange_calendars as exchange_calendars
from exchange_calendars.errors import CalendarError

from macro_platform.contracts.common import Region

ReportDayType = Literal["regular", "weekend", "regional_holiday"]
MarketDayStatus = Literal["scheduled_session", "weekend_closed", "exchange_holiday"]

REPORT_DAY_POLICY_ID = "cn_hk_us_report_day_v1"
ALWAYS_REQUIRED_REPORT_INPUT_IDS = frozenset(
    {
        "news.cn.official_headlines_24h",
        "news.hk.official_headlines_24h",
        "calendar.macro_releases_7d",
    }
)
MARKET_INPUT_ID_BY_REGION: dict[Region, str] = {
    Region.CN: "market.cn.core_indices.previous_close",
    Region.HK: "market.hk.core_indices.previous_close",
    Region.US: "market.us.core_indices.previous_close",
}
_VENUE_CALENDAR_BY_REGION: dict[Region, str] = {
    Region.CN: "XSHG",
    Region.HK: "XHKG",
    Region.US: "XNYS",
}


class ReportDayPolicyUnavailableError(ValueError):
    """Raised when a reviewed calendar cannot resolve the requested date."""


@dataclass(frozen=True, slots=True)
class RegionMarketDayPolicy:
    region: str
    calendar_name: str
    status: MarketDayStatus
    market_input_id: str
    market_input_required: bool
    previous_session: str


@dataclass(frozen=True, slots=True)
class ReportDayPolicyResult:
    policy_id: str
    calendar_version: str
    report_date: str
    day_type: ReportDayType
    required_input_ids: tuple[str, ...]
    optional_input_ids: tuple[str, ...]
    regions: tuple[RegionMarketDayPolicy, ...]

    def to_payload(self) -> dict[str, object]:
        return asdict(self)


class ReportDayPolicy(Protocol):
    def evaluate(self, report_date: date) -> ReportDayPolicyResult: ...


class ExchangeReportDayPolicy:
    """Resolve market requirements and persist the exact calendar decision."""

    def evaluate(self, report_date: date) -> ReportDayPolicyResult:
        weekend = report_date.weekday() >= 5
        regions = tuple(
            self._region_policy(region=region, report_date=report_date, weekend=weekend)
            for region in (Region.CN, Region.HK, Region.US)
        )
        required_market_inputs = {
            item.market_input_id for item in regions if item.market_input_required
        }
        optional_market_inputs = {
            item.market_input_id for item in regions if not item.market_input_required
        }
        day_type: ReportDayType
        if weekend:
            day_type = "weekend"
        elif optional_market_inputs:
            day_type = "regional_holiday"
        else:
            day_type = "regular"
        return ReportDayPolicyResult(
            policy_id=REPORT_DAY_POLICY_ID,
            calendar_version=version("exchange-calendars"),
            report_date=report_date.isoformat(),
            day_type=day_type,
            required_input_ids=tuple(
                sorted(ALWAYS_REQUIRED_REPORT_INPUT_IDS | required_market_inputs)
            ),
            optional_input_ids=tuple(sorted(optional_market_inputs)),
            regions=regions,
        )

    @staticmethod
    def _region_policy(
        *, region: Region, report_date: date, weekend: bool
    ) -> RegionMarketDayPolicy:
        calendar_name = _VENUE_CALENDAR_BY_REGION[region]
        try:
            calendar = exchange_calendars.get_calendar(calendar_name)
            is_session = bool(calendar.is_session(report_date))
            previous = (
                calendar.previous_session(report_date)
                if is_session
                else calendar.date_to_session(report_date, direction="previous")
            )
        except (CalendarError, ValueError) as exc:
            raise ReportDayPolicyUnavailableError(
                f"{calendar_name} cannot resolve report-day policy for {report_date.isoformat()}"
            ) from exc
        status: MarketDayStatus
        if weekend:
            status = "weekend_closed"
        elif not is_session:
            status = "exchange_holiday"
        else:
            status = "scheduled_session"
        return RegionMarketDayPolicy(
            region=region.value,
            calendar_name=calendar_name,
            status=status,
            market_input_id=MARKET_INPUT_ID_BY_REGION[region],
            market_input_required=status == "scheduled_session",
            previous_session=cast(date, previous.date()).isoformat(),
        )


def report_calendar_payload(
    raw_policy: object,
    *,
    report_date: date,
) -> dict[str, str | None]:
    """Translate the persisted policy into the frozen DailyReport calendar shape."""

    if not isinstance(raw_policy, dict) or raw_policy.get("report_date") != report_date.isoformat():
        return {
            "day_type": "weekend" if report_date.weekday() >= 5 else "business_day",
            "holiday_notice": None,
        }
    day_type = raw_policy.get("day_type")
    if day_type == "weekend":
        return {
            "day_type": "weekend",
            "holiday_notice": "周末，CN/HK/US 市场休市；本期发布宏观消息、日历和分析。",
        }
    if day_type == "regional_holiday":
        regions = raw_policy.get("regions")
        closed_regions = (
            sorted(
                str(item.get("region"))
                for item in regions
                if isinstance(item, dict) and item.get("status") == "exchange_holiday"
            )
            if isinstance(regions, (list, tuple))
            else []
        )
        if closed_regions:
            return {
                "day_type": "holiday",
                "holiday_notice": (
                    f"{', '.join(closed_regions)} 市场因交易所节假日休市；其他地区按正常输入门禁。"
                ),
            }
    return {"day_type": "business_day", "holiday_notice": None}


__all__ = [
    "ALWAYS_REQUIRED_REPORT_INPUT_IDS",
    "ExchangeReportDayPolicy",
    "MARKET_INPUT_ID_BY_REGION",
    "REPORT_DAY_POLICY_ID",
    "RegionMarketDayPolicy",
    "ReportDayPolicy",
    "ReportDayPolicyResult",
    "ReportDayPolicyUnavailableError",
    "report_calendar_payload",
]
