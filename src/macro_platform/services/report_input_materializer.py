"""Materialize immutable report inputs from durable ingestion evidence.

The quality gate deliberately consumes a snapshot rather than querying live
providers.  This module is the single writer that converts durable evidence
into that snapshot, so report validation cannot be made to pass by a caller
inventing a friendly ``input_quality`` payload.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from macro_platform.contracts.common import Region, SourceRef
from macro_platform.contracts.macro import MacroRelease
from macro_platform.contracts.market import MarketBar
from macro_platform.contracts.news import NewsEvent
from macro_platform.jobs.scheduler import ScheduledTaskResult
from macro_platform.normalization.common import canonical_json_checksum, stable_id, utc_now
from macro_platform.services.report_input_quality import (
    QualityGateResult,
    ReportInputQualityGate,
)
from macro_platform.storage.database import Database
from macro_platform.storage.models import (
    IngestRejectionRow,
    MacroReleaseRevisionRow,
    MacroReleaseRow,
    MarketBarRevisionRow,
    MarketBarRow,
    NewsEventRegionRow,
    NewsEventRow,
)
from macro_platform.storage.reporting import ReportInputSnapshot
from macro_platform.storage.repositories import ReportRepository
from macro_platform.storage.unit_of_work import UnitOfWork

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
    """One report input's typed facts and quality conclusion.

    ``facts`` and ``source_references`` are already canonical, machine-readable
    report payload fragments.  The production evidence reader derives them
    from database facts; callers cannot provide an untyped quality dictionary.
    """

    input_id: str
    status: InputEvidenceStatus
    required: bool
    reason: str
    facts: tuple[dict[str, object], ...] = ()
    source_references: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class MaterializedReportInput:
    snapshot: ReportInputSnapshot
    quality: QualityGateResult


class ReportInputEvidenceStore(Protocol):
    async def collect(
        self,
        *,
        report_date: date,
        as_of: datetime,
        cutoff_at: datetime,
        task_results: tuple[ScheduledTaskResult, ...],
    ) -> tuple[InputQualityEvidence, ...]: ...


class ReportInputSnapshotStore(Protocol):
    async def put(self, snapshot: ReportInputSnapshot) -> None: ...


@dataclass(frozen=True, slots=True)
class MarketInputSpec:
    input_id: str
    task_id: str
    region: Region
    expected_instrument_ids: frozenset[str]


_MARKET_INPUT_SPECS = (
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


class PostgresReportInputSnapshotStore:
    """Persist immutable snapshots through the existing report repository."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def put(self, snapshot: ReportInputSnapshot) -> None:
        async with UnitOfWork(self._database).transaction() as session:
            await ReportRepository(session).put_input_snapshot(snapshot)


class PostgresReportInputEvidenceStore:
    """Derive report-input evidence from normalized facts and durable task outcomes."""

    def __init__(
        self,
        database: Database,
        *,
        market_max_age: timedelta = timedelta(hours=72),
        news_max_age: timedelta = timedelta(hours=30),
        macro_max_age: timedelta = timedelta(days=8),
    ) -> None:
        if min(market_max_age, news_max_age, macro_max_age) <= timedelta(0):
            raise ValueError("report input freshness windows must be positive")
        self._database = database
        self._market_max_age = market_max_age
        self._news_max_age = news_max_age
        self._macro_max_age = macro_max_age

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
            rejections_by_run_id = await _rejections_by_run_id(session, task_results)
            market_evidence = [
                await self._market_evidence(
                    session,
                    spec=spec,
                    report_date=report_date,
                    as_of=as_of,
                    cutoff_at=cutoff_at,
                    task_result=results_by_task_id.get(spec.task_id),
                    rejection_count=_task_rejection_count(
                        results_by_task_id.get(spec.task_id), rejections_by_run_id
                    ),
                )
                for spec in _MARKET_INPUT_SPECS
            ]
            news_evidence = [
                await self._news_evidence(
                    session,
                    input_id=f"news.{region.value.lower()}.official_headlines_24h",
                    region=region,
                    report_date=report_date,
                    as_of=as_of,
                    cutoff_at=cutoff_at,
                )
                for region in (Region.CN, Region.HK)
            ]
            macro_evidence = await self._macro_evidence(
                session,
                report_date=report_date,
                as_of=as_of,
                cutoff_at=cutoff_at,
            )
        return tuple([*market_evidence, *news_evidence, macro_evidence])

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
        task_problem = _task_problem(spec.input_id, task_result, rejection_count=rejection_count)
        if task_problem is not None:
            return task_problem
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
        revised_ids = set(
            (
                await session.scalars(
                    select(MarketBarRevisionRow.bar_id).where(
                        MarketBarRevisionRow.bar_id.in_([bar.bar_id for bar in bars]),
                        MarketBarRevisionRow.available_at <= as_of,
                    )
                )
            ).all()
        )
        status, reason = _source_status(
            available_ats=tuple(bar.available_at for bar in bars),
            as_of=as_of,
            cutoff_at=cutoff_at,
            max_age=self._market_max_age,
            revised=bool(revised_ids),
        )
        fact_id = f"fact.{spec.input_id}"
        sources = tuple(_source_reference(bar.source) for bar in bars)
        return InputQualityEvidence(
            input_id=spec.input_id,
            status=status,
            required=True,
            reason=reason,
            facts=(
                {
                    "fact_id": fact_id,
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
    ) -> InputQualityEvidence:
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
        status, reason = _source_status(
            available_ats=tuple(event.available_at for event in events),
            as_of=as_of,
            cutoff_at=cutoff_at,
            max_age=self._news_max_age,
            revised=any(event.status == "corrected" for event in events),
        )
        sources = tuple(_source_reference(event.source) for event in events)
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
        report_date: date,
        as_of: datetime,
        cutoff_at: datetime,
    ) -> InputQualityEvidence:
        end_date = report_date + timedelta(days=7)
        rows = (
            await session.scalars(
                select(MacroReleaseRow)
                .where(
                    MacroReleaseRow.region.in_([Region.CN.value, Region.US.value]),
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
                input_id="calendar.macro_releases_7d",
                status="missing",
                required=True,
                reason="no CN or US macro releases materialized for the next seven days",
            )
        releases = tuple(MacroRelease.model_validate(row.payload) for row in rows)
        revised_ids = set(
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
        status, reason = _source_status(
            available_ats=tuple(release.available_at for release in releases),
            as_of=as_of,
            cutoff_at=cutoff_at,
            max_age=self._macro_max_age,
            revised=bool(revised_ids),
        )
        sources = tuple(_source_reference(release.source) for release in releases)
        return InputQualityEvidence(
            input_id="calendar.macro_releases_7d",
            status=status,
            required=True,
            reason=reason,
            facts=(
                {
                    "fact_id": "fact.calendar.macro_releases_7d",
                    "label": "CN and US macro releases in next seven days",
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


def _task_problem(
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


def _source_status(
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


async def _rejections_by_run_id(
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


def _task_rejection_count(
    task_result: ScheduledTaskResult | None,
    rejections_by_run_id: dict[UUID, int],
) -> int:
    if task_result is None:
        return 0
    persisted = 0 if task_result.run_id is None else rejections_by_run_id.get(task_result.run_id, 0)
    return max(task_result.records_rejected, persisted)


def _source_reference(source: SourceRef) -> dict[str, object]:
    return {
        "source_ref_id": stable_id(
            "report-source", source.provider_id, source.provider_record_id, source.checksum_sha256
        ),
        "provider_id": source.provider_id,
        "provider_record_id": source.provider_record_id,
        "source_name": source.source_name,
        "source_url": None if source.source_url is None else str(source.source_url),
        "retrieved_at": _isoformat(source.retrieved_at),
        "checksum_sha256": source.checksum_sha256,
    }


class ReportInputSnapshotMaterializer:
    """Build, quality-check and persist one immutable report input snapshot."""

    def __init__(
        self,
        *,
        evidence_store: ReportInputEvidenceStore,
        snapshot_store: ReportInputSnapshotStore,
        now: Callable[[], datetime] = utc_now,
        cutoff_at: Callable[[date], datetime],
        snapshot_version: str = "1.0",
    ) -> None:
        if not snapshot_version.strip():
            raise ValueError("snapshot version must not be empty")
        self._evidence_store = evidence_store
        self._snapshot_store = snapshot_store
        self._now = now
        self._cutoff_at = cutoff_at
        self._snapshot_version = snapshot_version
        self._quality_gate = ReportInputQualityGate()

    async def materialize(
        self,
        report_date: date,
        *,
        task_results: tuple[ScheduledTaskResult, ...],
    ) -> MaterializedReportInput:
        as_of = self._now().astimezone(UTC)
        cutoff_at = self._cutoff_at(report_date).astimezone(UTC)
        evidence = await self._evidence_store.collect(
            report_date=report_date,
            as_of=as_of,
            cutoff_at=cutoff_at,
            task_results=task_results,
        )
        snapshot = _snapshot_from_evidence(
            report_date=report_date,
            as_of=as_of,
            cutoff_at=cutoff_at,
            snapshot_version=self._snapshot_version,
            evidence=evidence,
        )
        quality = self._quality_gate.evaluate(snapshot)
        await self._snapshot_store.put(snapshot)
        return MaterializedReportInput(snapshot=snapshot, quality=quality)


def _snapshot_from_evidence(
    *,
    report_date: date,
    as_of: datetime,
    cutoff_at: datetime,
    snapshot_version: str,
    evidence: tuple[InputQualityEvidence, ...],
) -> ReportInputSnapshot:
    by_input_id = {item.input_id: item for item in evidence}
    if len(by_input_id) != len(evidence):
        raise ValueError("report input evidence IDs must be unique")
    facts = sorted(
        (fact for item in evidence for fact in item.facts),
        key=lambda fact: str(fact.get("fact_id", "")),
    )
    fact_ids = [str(fact["fact_id"]) for fact in facts if isinstance(fact.get("fact_id"), str)]
    if len(fact_ids) != len(facts) or len(fact_ids) != len(set(fact_ids)):
        raise ValueError("materialized report facts must have unique fact IDs")
    source_by_id: dict[str, dict[str, object]] = {}
    for source in (source for item in evidence for source in item.source_references):
        source_ref_id = source.get("source_ref_id")
        if not isinstance(source_ref_id, str):
            raise ValueError("materialized report source reference is missing source_ref_id")
        existing = source_by_id.setdefault(source_ref_id, source)
        if existing != source:
            raise ValueError("materialized report source reference ID has conflicting values")
    source_references = [source_by_id[source_id] for source_id in sorted(source_by_id)]
    input_quality = {
        input_id: {
            "status": item.status,
            "required": item.required,
            "reason": item.reason,
        }
        for input_id, item in sorted(by_input_id.items())
    }
    body = {
        "report_date": report_date.isoformat(),
        "as_of": _isoformat(as_of),
        "cutoff_at": _isoformat(cutoff_at),
        "snapshot_version": snapshot_version,
        "fact_ids": fact_ids,
        "facts": facts,
        "source_references": source_references,
        "input_quality": input_quality,
    }
    fingerprint = canonical_json_checksum(body)
    snapshot_id = stable_id("report-input", snapshot_version, report_date.isoformat(), fingerprint)
    payload = {
        "snapshot_id": snapshot_id,
        "snapshot_version": snapshot_version,
        "as_of": _isoformat(as_of),
        "cutoff_at": _isoformat(cutoff_at),
        "fingerprint_sha256": fingerprint,
        "fact_ids": fact_ids,
        "facts": facts,
        "source_references": source_references,
        "input_quality": input_quality,
    }
    return ReportInputSnapshot(
        snapshot_id=snapshot_id,
        snapshot_version=snapshot_version,
        report_date=report_date,
        as_of=as_of,
        cutoff_at=cutoff_at,
        fingerprint_sha256=fingerprint,
        fact_ids=fact_ids,
        payload=payload,
    )


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
