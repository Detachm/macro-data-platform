"""PostgreSQL reader that derives report-input quality from durable facts."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from macro_platform.contracts.common import Region, SourceRef
from macro_platform.contracts.market import MarketBar
from macro_platform.contracts.news import NewsEvent
from macro_platform.jobs.scheduler import ScheduledTaskResult
from macro_platform.normalization.common import stable_id
from macro_platform.services.report_input_evidence_queries import (
    has_late_macro_release,
    late_market_instruments,
    macro_has_revision_before_cutoff,
    macro_release_fact,
    macro_releases_as_of,
    market_bar_fact,
    market_bars_as_of,
    market_has_revision_before_cutoff,
    news_event_fact,
)
from macro_platform.services.report_input_evidence_support import (
    MARKET_INPUT_SPECS,
    ExchangeMarketSessionCalendar,
    InputQualityEvidence,
    MarketInputSpec,
    MarketSessionCalendar,
    MarketSessionCalendarUnavailableError,
    ReportInputEvidenceStore,
    committed_task_pages,
    news_task_id,
    rejections_by_run_id,
    source_reference,
    source_status,
    task_problem,
    task_rejection_count,
    unexpected_trading_dates,
)
from macro_platform.storage.database import Database
from macro_platform.storage.models import NewsEventRegionRow, NewsEventRow


class PostgresReportInputEvidenceStore(ReportInputEvidenceStore):
    """Derive report-input evidence from normalized facts and task outcomes."""

    def __init__(
        self,
        database: Database,
        *,
        market_max_age: timedelta = timedelta(hours=36),
        news_max_age: timedelta = timedelta(hours=24),
        market_session_calendar: MarketSessionCalendar | None = None,
    ) -> None:
        if min(market_max_age, news_max_age) <= timedelta(0):
            raise ValueError("report input freshness windows must be positive")
        self._database = database
        self._market_max_age = market_max_age
        self._news_max_age = news_max_age
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
                "market.hk.core_indices.previous_close",
                "XtQuant live coverage contains approved HK equities, not the required "
                "HK core-index input",
            ),
            _unsupported_live_input(
                "news.cn.official_headlines_24h",
                "CN official headlines have no approved live provider",
            ),
            hk_news,
            _global_calendar_evidence(
                cn_macro,
                missing_regions=(Region.HK, Region.US),
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
        if task_result is None:
            raise AssertionError("a successful task result is required after task_problem")
        commits = await committed_task_pages(session, task_result)
        if commits.page_count == 0:
            return _unavailable_evidence(
                spec.input_id,
                "scheduled task has no committed provider pages for its durable run evidence",
            )
        if not commits.provider_record_ids:
            return InputQualityEvidence(
                input_id=spec.input_id,
                status="missing",
                required=True,
                reason="scheduled task committed no market-bar provider records",
            )
        rows = await market_bars_as_of(
            session,
            region=spec.region,
            instrument_ids=spec.expected_instrument_ids,
            provider_record_ids=commits.provider_record_ids,
            run_ids=task_result.evidence_run_ids,
            available_at=cutoff_at,
            report_date=report_date,
        )
        selected: dict[str, MarketBar] = {}
        for bar in rows:
            selected.setdefault(bar.instrument_id, bar)
        missing = sorted(spec.expected_instrument_ids - set(selected))
        try:
            expected = self._market_session_calendar.previous_session(
                region=spec.region, report_date=report_date
            )
        except MarketSessionCalendarUnavailableError as exc:
            return _unavailable_evidence(spec.input_id, str(exc))
        if missing:
            late_instruments = await late_market_instruments(
                session,
                region=spec.region,
                provider_record_ids=commits.provider_record_ids,
                run_ids=task_result.evidence_run_ids,
                cutoff_at=cutoff_at,
                as_of=as_of,
                expected_trading_date=expected,
            )
            if set(missing).issubset(late_instruments):
                return InputQualityEvidence(
                    input_id=spec.input_id,
                    status="late",
                    required=True,
                    reason=(
                        "required market bars were committed after the report cutoff: "
                        + ", ".join(missing)
                    ),
                )
            return InputQualityEvidence(
                input_id=spec.input_id,
                status="missing",
                required=True,
                reason=f"missing expected instruments: {', '.join(missing)}",
            )
        bars = tuple(selected[instrument_id] for instrument_id in sorted(selected))
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
        revised = await market_has_revision_before_cutoff(
            session,
            selected_versions={(bar.bar_id, bar.source.checksum_sha256) for bar in bars},
            cutoff_at=cutoff_at,
        )
        status, reason = source_status(
            available_ats=tuple(bar.available_at for bar in bars),
            cutoff_at=cutoff_at,
            max_age=self._market_max_age,
            revised=revised,
        )
        sources = tuple(source_reference(bar.source) for bar in bars)
        return InputQualityEvidence(
            input_id=spec.input_id,
            status=status,
            required=True,
            reason=reason,
            facts=tuple(
                market_bar_fact(
                    bar,
                    input_id=spec.input_id,
                    report_date=report_date,
                    source_ref_id=_source_ref_id(bar.source),
                )
                for bar in bars
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
        if task_result is None:
            raise AssertionError("a successful task result is required after task_problem")
        commits = await committed_task_pages(session, task_result)
        if commits.page_count == 0:
            return _unavailable_evidence(
                input_id,
                "scheduled task has no committed provider pages for its durable run evidence",
            )
        window_start = cutoff_at - timedelta(hours=24)
        rows = (
            await session.scalars(
                select(NewsEventRow)
                .join(NewsEventRegionRow, NewsEventRegionRow.news_id == NewsEventRow.news_id)
                .where(
                    NewsEventRegionRow.region == region.value,
                    NewsEventRow.status.in_(("active", "corrected")),
                    NewsEventRow.provider_record_id.in_(sorted(commits.provider_record_ids)),
                    NewsEventRow.available_at <= cutoff_at,
                    or_(
                        NewsEventRow.published_at >= window_start,
                        NewsEventRow.published_date >= window_start.date(),
                    ),
                )
                .order_by(NewsEventRow.available_at.desc(), NewsEventRow.news_id)
            )
        ).all()
        if not rows:
            late_rows = (
                await session.scalars(
                    select(NewsEventRow.news_id)
                    .join(
                        NewsEventRegionRow,
                        NewsEventRegionRow.news_id == NewsEventRow.news_id,
                    )
                    .where(
                        NewsEventRegionRow.region == region.value,
                        NewsEventRow.status.in_(("active", "corrected")),
                        NewsEventRow.provider_record_id.in_(sorted(commits.provider_record_ids)),
                        NewsEventRow.available_at > cutoff_at,
                        NewsEventRow.available_at <= as_of,
                        or_(
                            NewsEventRow.published_at >= window_start,
                            NewsEventRow.published_date >= window_start.date(),
                        ),
                    )
                )
            ).all()
            if late_rows:
                return InputQualityEvidence(
                    input_id=input_id,
                    status="late",
                    required=True,
                    reason="official headlines were committed after the report cutoff",
                )
            return InputQualityEvidence(
                input_id=input_id,
                status="missing",
                required=True,
                reason="no official headline materialized in the last 24 hours",
            )
        events = tuple(NewsEvent.model_validate(row.payload) for row in rows)
        status, reason = source_status(
            available_ats=tuple(event.available_at for event in events),
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
            facts=tuple(
                news_event_fact(
                    event,
                    input_id=input_id,
                    report_date=report_date,
                    source_ref_id=_source_ref_id(event.source),
                )
                for event in events
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
        if task_result is None:
            raise AssertionError("a successful task result is required after task_problem")
        commits = await committed_task_pages(session, task_result)
        if commits.page_count == 0:
            return _unavailable_evidence(
                input_id,
                "scheduled task has no committed provider pages for its durable run evidence",
            )
        releases = await macro_releases_as_of(
            session,
            region=region,
            provider_record_ids=commits.provider_record_ids,
            run_ids=task_result.evidence_run_ids,
            report_date=report_date,
            cutoff_at=cutoff_at,
        )
        if not releases:
            late = await has_late_macro_release(
                session,
                region=region,
                provider_record_ids=commits.provider_record_ids,
                run_ids=task_result.evidence_run_ids,
                report_date=report_date,
                cutoff_at=cutoff_at,
                as_of=as_of,
            )
            if late:
                return InputQualityEvidence(
                    input_id=input_id,
                    status="late",
                    required=True,
                    reason="macro release calendar entries were committed after the report cutoff",
                )
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
                        "fact_id": f"fact.{input_id}.empty",
                        "input_id": input_id,
                        "fact_type": "macro_release_calendar",
                        "label": f"{region.value} macro releases in next seven days",
                        "display_text": "No macro releases are scheduled in the next seven days.",
                        "value": 0,
                        "unit": "events",
                        "available_at": cutoff_at.isoformat().replace("+00:00", "Z"),
                        "report_date": report_date.isoformat(),
                        "source_ref_ids": [],
                    },
                ),
            )
        revised = await macro_has_revision_before_cutoff(
            session,
            selected_versions={
                (release.release_id, release.source.checksum_sha256) for release in releases
            },
            cutoff_at=cutoff_at,
        )
        status: Literal["available", "revised"] = "revised" if revised else "available"
        reason = (
            "a later source revision exists for this report input"
            if revised
            else "calendar coverage is materialized before cutoff"
        )
        sources = tuple(source_reference(release.source) for release in releases)
        return InputQualityEvidence(
            input_id=input_id,
            status=status,
            required=True,
            reason=reason,
            facts=tuple(
                macro_release_fact(
                    release,
                    input_id=input_id,
                    report_date=report_date,
                    source_ref_id=_source_ref_id(release.source),
                )
                for release in releases
            ),
            source_references=sources,
        )


def _unavailable_evidence(input_id: str, reason: str) -> InputQualityEvidence:
    return InputQualityEvidence(
        input_id=input_id,
        status="unavailable",
        required=True,
        reason=reason,
    )


def _source_ref_id(source: SourceRef) -> str:
    return stable_id(
        "report-source", source.provider_id, source.provider_record_id, source.checksum_sha256
    )


def _global_calendar_evidence(
    cn_evidence: InputQualityEvidence,
    *,
    missing_regions: tuple[Region, ...],
) -> InputQualityEvidence:
    """Keep the frozen v1 aggregate calendar input fail-closed by region."""

    if cn_evidence.status not in {"available", "revised"}:
        return cn_evidence
    return InputQualityEvidence(
        input_id=cn_evidence.input_id,
        status="missing",
        required=True,
        reason=(
            "no approved live macro-release calendar provider for "
            + ", ".join(region.value for region in missing_regions)
        ),
    )


def _unsupported_live_input(input_id: str, reason: str) -> InputQualityEvidence:
    """Make an unapproved provider gap visible instead of trusting old rows."""

    return InputQualityEvidence(
        input_id=input_id,
        status="missing",
        required=True,
        reason=reason,
    )
