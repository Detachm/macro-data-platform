"""Typed report-input evidence contracts and pure quality helpers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal, Protocol, cast
from uuid import UUID

import exchange_calendars as exchange_calendars
from exchange_calendars.errors import CalendarError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from macro_platform.contracts.common import Region, SourceRef
from macro_platform.jobs.scheduler import ScheduledTaskResult
from macro_platform.normalization.common import stable_id
from macro_platform.storage.models import IngestRejectionRow

InputEvidenceStatus = Literal[
    "available",
    "revised",
    "retryable",
    "missing",
    "stale",
    "late",
    "unavailable",
    "quarantined",
    "invalid",
]


@dataclass(frozen=True, slots=True)
class InputQualityEvidence:
    """One input's typed facts, provenance, and quality conclusion."""

    input_id: str
    status: InputEvidenceStatus
    required: bool
    reason: str
    facts: tuple[dict[str, object], ...] = ()
    source_references: tuple[dict[str, object], ...] = ()


class ReportInputEvidenceStore(Protocol):
    async def collect(
        self,
        *,
        report_date: date,
        as_of: datetime,
        cutoff_at: datetime,
        task_results: tuple[ScheduledTaskResult, ...],
    ) -> tuple[InputQualityEvidence, ...]: ...


class MarketSessionCalendar(Protocol):
    """Resolve the last expected market session for one report date."""

    def previous_session(self, *, region: Region, report_date: date) -> date: ...


class MarketSessionCalendarUnavailableError(ValueError):
    """Raised when the approved venue calendar cannot resolve a report date."""


_VENUE_CALENDAR_BY_REGION: dict[Region, str] = {
    Region.CN: "XSHG",
    Region.HK: "XHKG",
    Region.US: "XNYS",
}


class ExchangeMarketSessionCalendar:
    """Resolve prior sessions through reviewed exchange holiday calendars.

    The mapping deliberately uses XSHG, XHKG and XNYS rather than a weekday
    approximation. Calendar coverage gaps fail closed so an unreviewed future
    holiday cannot be mistaken for a market session.
    """

    def previous_session(self, *, region: Region, report_date: date) -> date:
        calendar_name = _VENUE_CALENDAR_BY_REGION[region]
        try:
            calendar = exchange_calendars.get_calendar(calendar_name)
            if calendar.is_session(report_date):
                previous = calendar.previous_session(report_date)
            else:
                previous = calendar.date_to_session(report_date, direction="previous")
        except (CalendarError, ValueError) as exc:
            raise MarketSessionCalendarUnavailableError(
                f"{calendar_name} cannot resolve a previous session for {report_date.isoformat()}"
            ) from exc
        return cast(date, previous.date())


@dataclass(frozen=True, slots=True)
class MarketInputSpec:
    input_id: str
    task_id: str
    region: Region
    expected_instrument_ids: frozenset[str]


MARKET_INPUT_SPECS = (
    MarketInputSpec(
        input_id="market.cn.core_indices.previous_close",
        task_id="cn.daily-bars",
        region=Region.CN,
        expected_instrument_ids=frozenset(
            {
                "ins_cn_index_sse_composite",
                "ins_cn_index_csi300",
                "ins_cn_index_szse_component",
            }
        ),
    ),
    MarketInputSpec(
        input_id="market.hk.core_indices.previous_close",
        task_id="hk.daily-bars",
        region=Region.HK,
        expected_instrument_ids=frozenset(
            {
                "ins_hk_equity_00700",
                "ins_hk_equity_09988",
                "ins_hk_equity_03690",
                "ins_hk_equity_01810",
                "ins_hk_equity_00941",
                "ins_hk_equity_00005",
                "ins_hk_equity_00388",
                "ins_hk_equity_01299",
                "ins_hk_equity_02318",
                "ins_hk_equity_09618",
            }
        ),
    ),
    MarketInputSpec(
        input_id="market.us.core_indices.previous_close",
        task_id="us.daily-bars",
        region=Region.US,
        expected_instrument_ids=frozenset({"ins_us_etf_spy", "ins_us_etf_qqq", "ins_us_etf_dia"}),
    ),
)


def task_problem(
    input_id: str,
    task_result: ScheduledTaskResult | None,
    *,
    rejection_count: int,
) -> InputQualityEvidence | None:
    if task_result is None:
        return None
    if rejection_count > 0:
        return InputQualityEvidence(
            input_id=input_id,
            status="quarantined",
            required=True,
            reason=f"scheduled task quarantined {rejection_count} records",
        )
    if task_result.status == "succeeded":
        return None
    return InputQualityEvidence(
        input_id=input_id,
        status="retryable" if task_result.status == "retryable" else "unavailable",
        required=True,
        reason=(
            f"scheduled task {task_result.task_id} ended as {task_result.status}"
            + (f" ({task_result.error_code})" if task_result.error_code else "")
        ),
    )


def news_task_id(region: Region) -> str:
    return "hk.official-headlines" if region is Region.HK else "cn.official-headlines"


def unexpected_trading_dates(
    trading_dates: Iterable[date],
    *,
    expected_trading_date: date,
) -> list[date]:
    return sorted(
        {trading_date for trading_date in trading_dates if trading_date != expected_trading_date}
    )


def source_status(
    *,
    available_ats: tuple[datetime, ...],
    as_of: datetime,
    cutoff_at: datetime,
    max_age: timedelta,
    revised: bool,
) -> tuple[InputEvidenceStatus, str]:
    if any(available_at > cutoff_at for available_at in available_ats):
        return "late", "a required fact became available after the report cutoff"
    if any(as_of - available_at > max_age for available_at in available_ats):
        return "stale", "a required fact exceeds the configured freshness window"
    if revised:
        return "revised", "a later source revision exists for this report input"
    return "available", "all required facts are materialized before cutoff"


async def rejections_by_run_id(
    session: AsyncSession,
    task_results: tuple[ScheduledTaskResult, ...],
) -> dict[UUID, int]:
    run_ids = [result.run_id for result in task_results if result.run_id is not None]
    if not run_ids:
        return {}
    rows = await session.execute(
        select(IngestRejectionRow.run_id, func.count(IngestRejectionRow.id))
        .where(IngestRejectionRow.run_id.in_(run_ids))
        .group_by(IngestRejectionRow.run_id)
    )
    return {run_id: count for run_id, count in rows.all()}


def task_rejection_count(
    task_result: ScheduledTaskResult | None,
    persisted_rejections: dict[UUID, int],
) -> int:
    if task_result is None:
        return 0
    persisted = 0 if task_result.run_id is None else persisted_rejections.get(task_result.run_id, 0)
    return max(task_result.records_rejected, persisted)


def source_reference(source: SourceRef) -> dict[str, object]:
    return {
        "source_ref_id": stable_id(
            "report-source", source.provider_id, source.provider_record_id, source.checksum_sha256
        ),
        "provider_id": source.provider_id,
        "provider_record_id": source.provider_record_id,
        "source_name": source.source_name,
        "source_url": None if source.source_url is None else str(source.source_url),
        "retrieved_at": source.retrieved_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "checksum_sha256": source.checksum_sha256,
    }
