"""PostgreSQL reader that derives report-input quality from durable facts."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from macro_platform.contracts.common import Region
from macro_platform.contracts.macro import MacroRelease
from macro_platform.contracts.market import MarketBar
from macro_platform.contracts.news import NewsEvent
from macro_platform.jobs.scheduler import ScheduledTaskResult
from macro_platform.services.report_input_evidence_support import (
    MARKET_INPUT_SPECS,
    ExchangeMarketSessionCalendar,
    InputQualityEvidence,
    MarketInputSpec,
    MarketSessionCalendar,
    MarketSessionCalendarUnavailableError,
    ReportInputEvidenceStore,
    news_task_id,
    rejections_by_run_id,
    source_reference,
    source_status,
    task_problem,
    task_rejection_count,
    unexpected_trading_dates,
)
from macro_platform.storage.database import Database
from macro_platform.storage.models import (
    MacroReleaseRevisionRow,
    MacroReleaseRow,
    MarketBarRevisionRow,
    MarketBarRow,
    NewsEventRegionRow,
    NewsEventRow,
)


class PostgresReportInputEvidenceStore(ReportInputEvidenceStore):
    """Derive report-input evidence from normalized facts and task outcomes."""

    def __init__(
        self,
        database: Database,
        *,
        market_max_age: timedelta = timedelta(hours=72),
        news_max_age: timedelta = timedelta(hours=30),
        macro_max_age: timedelta = timedelta(days=8),
        market_session_calendar: MarketSessionCalendar | None = None,
    ) -> None:
        if min(market_max_age, news_max_age, macro_max_age) <= timedelta(0):
            raise ValueError("report input freshness windows must be positive")
        self._database = database
        self._market_max_age = market_max_age
        self._news_max_age = news_max_age
        self._macro_max_age = macro_max_age
        self._market_session_calendar = market_session_calendar or ExchangeMarketSessionCalendar()

    async def collect(
        self,
        *,
        report_date: date,
        as_of: datetime,
        cutoff_at: datetime,
        task_results: tuple[ScheduledTaskResult, ...],
    ) -> tuple[InputQualityEvidence, ...]:
        results_by_task_id = {result.task_id: result for result in task_results}
        if len(results_by_task_id) != len(task_results):
            raise ValueError("scheduled task results must have unique task IDs")
        async with self._database.session() as session:
            rejections = await rejections_by_run_id(session, task_results)
            market = [
                await self._market_evidence(
                    session,
                    spec=spec,
                    report_date=report_date,
                    as_of=as_of,
                    cutoff_at=cutoff_at,
                    task_result=results_by_task_id.get(spec.task_id),
                    rejection_count=task_rejection_count(
                        results_by_task_id.get(spec.task_id), rejections
                    ),
                )
                for spec in MARKET_INPUT_SPECS
            ]
            hk_news = await self._news_evidence(
                session,
                input_id="news.hk.official_headlines_24h",
                region=Region.HK,
                report_date=report_date,
                as_of=as_of,
                cutoff_at=cutoff_at,
                task_result=results_by_task_id.get(news_task_id(Region.HK)),
                rejection_count=task_rejection_count(
                    results_by_task_id.get(news_task_id(Region.HK)), rejections
                ),
            )
            cn_macro = await self._macro_evidence(
                session,
                input_id="calendar.macro_releases_7d",
                region=Region.CN,
                report_date=report_date,
                as_of=as_of,
                cutoff_at=cutoff_at,
                task_result=results_by_task_id.get("cn.macro-release-calendar"),
                rejection_count=task_rejection_count(
                    results_by_task_id.get("cn.macro-release-calendar"), rejections
                ),
            )
        return (
            *market,
            _unsupported_live_input(
                "news.cn.official_headlines_24h",
                "CN official headlines have no approved live provider",
            ),
            hk_news,
            cn_macro,
            _unsupported_live_input(
                "calendar.us_macro_releases_7d",
                "US macro release calendar has no approved live provider",
            ),
        )

    async def _market_evidence(
        self,
        session: AsyncSession,
        *,
        spec: MarketInputSpec,
        report_date: date,
        as_of: datetime,
        cutoff_at: datetime,
        task_result: ScheduledTaskResult | None,
        rejection_count: int,
    ) -> InputQualityEvidence:
        problem = task_problem(spec.input_id, task_result, rejection_count=rejection_count)
        if problem is not None:
            return problem
        rows = (
            await session.scalars(
                select(MarketBarRow)
                .where(
                    MarketBarRow.region == spec.region.value,
                    MarketBarRow.instrument_id.in_(sorted(spec.expected_instrument_ids)),
                    MarketBarRow.trading_date < report_date,
                    MarketBarRow.available_at <= as_of,
                )
                .order_by(
                    MarketBarRow.instrument_id,
                    MarketBarRow.trading_date.desc(),
                    MarketBarRow.available_at.desc(),
                    MarketBarRow.bar_id.desc(),
                )
            )
        ).all()
        selected: dict[str, MarketBar] = {}
        for row in rows:
            if row.instrument_id not in selected:
                selected[row.instrument_id] = MarketBar.model_validate(row.payload)
        missing = sorted(spec.expected_instrument_ids - set(selected))
        if missing:
            return InputQualityEvidence(
                input_id=spec.input_id,
                status="missing",
                required=True,
                reason=f"missing expected instruments: {', '.join(missing)}",
            )
        bars = tuple(selected[instrument_id] for instrument_id in sorted(selected))
        try:
            expected = self._market_session_calendar.previous_session(
                region=spec.region, report_date=report_date
            )
        except MarketSessionCalendarUnavailableError as exc:
            return InputQualityEvidence(
                input_id=spec.input_id,
                status="unavailable",
                required=True,
                reason=str(exc),
            )
        stale_dates = unexpected_trading_dates(
            (bar.trading_date for bar in bars), expected_trading_date=expected
        )
        if stale_dates:
            return InputQualityEvidence(
                input_id=spec.input_id,
                status="stale",
                required=True,
                reason=(
                    f"expected previous market session {expected.isoformat()}, found "
                    f"{', '.join(value.isoformat() for value in stale_dates)}"
                ),
            )
        revised = set(
            (
                await session.scalars(
                    select(MarketBarRevisionRow.bar_id).where(
                        MarketBarRevisionRow.bar_id.in_([bar.bar_id for bar in bars]),
                        MarketBarRevisionRow.available_at <= as_of,
                    )
                )
            ).all()
        )
        status, reason = source_status(
            available_ats=tuple(bar.available_at for bar in bars),
            as_of=as_of,
            cutoff_at=cutoff_at,
            max_age=self._market_max_age,
            revised=bool(revised),
        )
        sources = tuple(source_reference(bar.source) for bar in bars)
        return InputQualityEvidence(
            input_id=spec.input_id,
            status=status,
            required=True,
            reason=reason,
            facts=(
                {
                    "fact_id": f"fact.{spec.input_id}",
                    "label": f"{spec.region.value} previous close coverage",
                    "display_text": (
                        f"{spec.region.value} previous-close coverage: {len(bars)} instruments."
                    ),
                    "value": len(bars),
                    "unit": "instruments",
                    "available_at": _isoformat(max(bar.available_at for bar in bars)),
                    "report_date": report_date.isoformat(),
                    "source_ref_ids": [source["source_ref_id"] for source in sources],
                },
            ),
            source_references=sources,
        )

    async def _news_evidence(
        self,
        session: AsyncSession,
        *,
        input_id: str,
        region: Region,
        report_date: date,
        as_of: datetime,
        cutoff_at: datetime,
        task_result: ScheduledTaskResult | None,
        rejection_count: int,
    ) -> InputQualityEvidence:
        problem = task_problem(input_id, task_result, rejection_count=rejection_count)
        if problem is not None:
            return problem
        window_start = cutoff_at - timedelta(hours=24)
        rows = (
            await session.scalars(
                select(NewsEventRow)
                .join(NewsEventRegionRow, NewsEventRegionRow.news_id == NewsEventRow.news_id)
                .where(
                    NewsEventRegionRow.region == region.value,
                    NewsEventRow.status.in_(("active", "corrected")),
                    NewsEventRow.available_at <= as_of,
                    or_(
                        NewsEventRow.published_at >= window_start,
                        NewsEventRow.published_date >= window_start.date(),
                    ),
                )
                .order_by(NewsEventRow.available_at.desc(), NewsEventRow.news_id)
            )
        ).all()
        if not rows:
            return InputQualityEvidence(
                input_id=input_id,
                status="missing",
                required=True,
                reason="no official headline materialized in the last 24 hours",
            )
        events = tuple(NewsEvent.model_validate(row.payload) for row in rows)
        status, reason = source_status(
            available_ats=tuple(event.available_at for event in events),
            as_of=as_of,
            cutoff_at=cutoff_at,
            max_age=self._news_max_age,
            revised=any(event.status == "corrected" for event in events),
        )
        sources = tuple(source_reference(event.source) for event in events)
        return InputQualityEvidence(
            input_id=input_id,
            status=status,
            required=True,
            reason=reason,
            facts=(
                {
                    "fact_id": f"fact.{input_id}",
                    "label": f"{region.value} official headlines",
                    "display_text": f"{len(events)} official headlines materialized.",
                    "value": len(events),
                    "unit": "events",
                    "available_at": _isoformat(max(event.available_at for event in events)),
                    "report_date": report_date.isoformat(),
                    "source_ref_ids": [source["source_ref_id"] for source in sources],
                },
            ),
            source_references=sources,
        )

    async def _macro_evidence(
        self,
        session: AsyncSession,
        *,
        input_id: str,
        region: Region,
        report_date: date,
        as_of: datetime,
        cutoff_at: datetime,
        task_result: ScheduledTaskResult | None,
        rejection_count: int,
    ) -> InputQualityEvidence:
        problem = task_problem(input_id, task_result, rejection_count=rejection_count)
        if problem is not None:
            return problem
        end_date = report_date + timedelta(days=7)
        rows = (
            await session.scalars(
                select(MacroReleaseRow)
                .where(
                    MacroReleaseRow.region == region.value,
                    MacroReleaseRow.available_at <= as_of,
                    or_(
                        (MacroReleaseRow.scheduled_at >= cutoff_at)
                        & (MacroReleaseRow.scheduled_at < cutoff_at + timedelta(days=7)),
                        (MacroReleaseRow.scheduled_date >= report_date)
                        & (MacroReleaseRow.scheduled_date < end_date),
                    ),
                )
                .order_by(MacroReleaseRow.available_at.desc(), MacroReleaseRow.release_id)
            )
        ).all()
        if not rows:
            return InputQualityEvidence(
                input_id=input_id,
                status="available",
                required=True,
                reason=(
                    f"{region.value} calendar coverage completed with no releases in the next "
                    "seven days"
                ),
                facts=(
                    {
                        "fact_id": f"fact.{input_id}",
                        "label": f"{region.value} macro releases in next seven days",
                        "display_text": "No macro releases are scheduled in the next seven days.",
                        "value": 0,
                        "unit": "events",
                        "report_date": report_date.isoformat(),
                        "source_ref_ids": [],
                    },
                ),
            )
        releases = tuple(MacroRelease.model_validate(row.payload) for row in rows)
        revised = set(
            (
                await session.scalars(
                    select(MacroReleaseRevisionRow.release_id).where(
                        MacroReleaseRevisionRow.release_id.in_(
                            [release.release_id for release in releases]
                        ),
                        MacroReleaseRevisionRow.available_at <= as_of,
                    )
                )
            ).all()
        )
        status, reason = source_status(
            available_ats=tuple(release.available_at for release in releases),
            as_of=as_of,
            cutoff_at=cutoff_at,
            max_age=self._macro_max_age,
            revised=bool(revised),
        )
        sources = tuple(source_reference(release.source) for release in releases)
        return InputQualityEvidence(
            input_id=input_id,
            status=status,
            required=True,
            reason=reason,
            facts=(
                {
                    "fact_id": f"fact.{input_id}",
                    "label": f"{region.value} macro releases in next seven days",
                    "display_text": f"{len(releases)} macro releases materialized.",
                    "value": len(releases),
                    "unit": "events",
                    "available_at": _isoformat(max(release.available_at for release in releases)),
                    "report_date": report_date.isoformat(),
                    "source_ref_ids": [source["source_ref_id"] for source in sources],
                },
            ),
            source_references=sources,
        )


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _unsupported_live_input(input_id: str, reason: str) -> InputQualityEvidence:
    """Make an unapproved provider gap visible instead of trusting old rows."""

    return InputQualityEvidence(
        input_id=input_id,
        status="missing",
        required=True,
        reason=reason,
    )
