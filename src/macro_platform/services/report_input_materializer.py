"""Materialize immutable report snapshots from typed durable evidence."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Protocol

from macro_platform.jobs.scheduler import ScheduledTaskResult
from macro_platform.normalization.common import canonical_json_checksum, stable_id, utc_now
from macro_platform.services.report_day_policy import (
    ExchangeReportDayPolicy,
    ReportDayPolicy,
    ReportDayPolicyResult,
)
from macro_platform.services.report_input_evidence import PostgresReportInputEvidenceStore
from macro_platform.services.report_input_evidence_support import (
    ExchangeMarketSessionCalendar,
    InputQualityEvidence,
    MarketSessionCalendar,
    ReportInputEvidenceStore,
)
from macro_platform.services.report_input_evidence_support import (
    unexpected_trading_dates as _unexpected_trading_dates,
)
from macro_platform.services.report_input_quality import (
    QualityGateResult,
    ReportInputQualityGate,
)
from macro_platform.storage.database import Database
from macro_platform.storage.reporting import ReportInputSnapshot
from macro_platform.storage.repositories import ReportRepository
from macro_platform.storage.unit_of_work import UnitOfWork


@dataclass(frozen=True, slots=True)
class MaterializedReportInput:
    snapshot: ReportInputSnapshot
    quality: QualityGateResult


class ReportInputSnapshotStore(Protocol):
    async def put(self, snapshot: ReportInputSnapshot) -> None: ...


class PostgresReportInputSnapshotStore:
    """Persist immutable snapshots through the existing report repository."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def put(self, snapshot: ReportInputSnapshot) -> None:
        async with UnitOfWork(self._database).transaction() as session:
            await ReportRepository(session).put_input_snapshot(snapshot)


class ReportInputSnapshotMaterializer:
    """Build, quality-check and persist one immutable report input snapshot."""

    def __init__(
        self,
        *,
        evidence_store: ReportInputEvidenceStore,
        snapshot_store: ReportInputSnapshotStore,
        now: Callable[[], datetime] = utc_now,
        cutoff_at: Callable[[date], datetime],
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        snapshot_version: str = "1.0",
        report_day_policy: ReportDayPolicy | None = None,
    ) -> None:
        if not snapshot_version.strip():
            raise ValueError("snapshot version must not be empty")
        self._evidence_store = evidence_store
        self._snapshot_store = snapshot_store
        self._now = now
        self._cutoff_at = cutoff_at
        self._sleeper = sleeper
        self._snapshot_version = snapshot_version
        self._report_day_policy = report_day_policy or ExchangeReportDayPolicy()
        self._quality_gate = ReportInputQualityGate()

    async def materialize(
        self,
        report_date: date,
        *,
        task_results: tuple[ScheduledTaskResult, ...],
    ) -> MaterializedReportInput:
        cutoff_at = self._cutoff_at(report_date).astimezone(UTC)
        as_of = self._now().astimezone(UTC)
        if as_of < cutoff_at:
            await self._sleeper((cutoff_at - as_of).total_seconds())
            as_of = self._now().astimezone(UTC)
            if as_of < cutoff_at:
                raise RuntimeError("materializer sleeper returned before the report cutoff")
        evidence = await self._evidence_store.collect(
            report_date=report_date,
            as_of=as_of,
            cutoff_at=cutoff_at,
            task_results=task_results,
        )
        report_day_policy = self._report_day_policy.evaluate(report_date)
        policy_evidence = _apply_report_day_policy(evidence, report_day_policy)
        snapshot = _snapshot_from_evidence(
            report_date=report_date,
            as_of=as_of,
            cutoff_at=cutoff_at,
            snapshot_version=self._snapshot_version,
            evidence=policy_evidence,
            report_day_policy=report_day_policy,
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
    report_day_policy: ReportDayPolicyResult,
) -> ReportInputSnapshot:
    by_input_id = {item.input_id: item for item in evidence}
    if len(by_input_id) != len(evidence):
        raise ValueError("report input evidence IDs must be unique")
    cutoff_safe_evidence = tuple(
        _exclude_after_cutoff_evidence(item, cutoff_at=cutoff_at) for item in evidence
    )
    usable_evidence = tuple(
        item for item in cutoff_safe_evidence if item.status in {"available", "revised"}
    )
    facts = sorted(
        (fact for item in usable_evidence for fact in item.facts),
        key=lambda fact: str(fact.get("fact_id", "")),
    )
    fact_ids = [str(fact["fact_id"]) for fact in facts if isinstance(fact.get("fact_id"), str)]
    if len(fact_ids) != len(facts) or len(fact_ids) != len(set(fact_ids)):
        raise ValueError("materialized report facts must have unique fact IDs")
    source_by_id: dict[str, dict[str, object]] = {}
    for source in (source for item in usable_evidence for source in item.source_references):
        source_ref_id = source.get("source_ref_id")
        if not isinstance(source_ref_id, str):
            raise ValueError("materialized report source reference is missing source_ref_id")
        existing = source_by_id.setdefault(source_ref_id, source)
        if existing != source:
            raise ValueError("materialized report source reference ID has conflicting values")
    source_references = [source_by_id[source_id] for source_id in sorted(source_by_id)]
    source_ref_ids = [source["source_ref_id"] for source in source_references]
    input_quality = {
        input_id: {
            "status": item.status,
            "required": item.required,
            "reason": item.reason,
        }
        for input_id, item in sorted({item.input_id: item for item in cutoff_safe_evidence}.items())
    }
    editor_context = _editor_context(
        facts=facts,
        source_references=source_references,
        input_quality=input_quality,
        report_day_policy=report_day_policy.to_payload(),
    )
    report_day_policy_payload = report_day_policy.to_payload()
    body = {
        "report_date": report_date.isoformat(),
        "as_of": _isoformat(as_of),
        "cutoff_at": _isoformat(cutoff_at),
        "snapshot_version": snapshot_version,
        "fact_ids": fact_ids,
        "facts": facts,
        "source_ref_ids": source_ref_ids,
        "source_references": source_references,
        "input_quality": input_quality,
        "report_day_policy": report_day_policy_payload,
        "editor_context": editor_context,
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
        "source_ref_ids": source_ref_ids,
        "source_references": source_references,
        "input_quality": input_quality,
        "report_day_policy": report_day_policy_payload,
        "editor_context": editor_context,
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


def _editor_context(
    *,
    facts: list[dict[str, object]],
    source_references: list[dict[str, object]],
    input_quality: Mapping[str, Mapping[str, object]],
    report_day_policy: Mapping[str, object],
) -> dict[str, object]:
    """Build the fixed provenance-preserving payload sent to report generation."""

    return {
        "facts": facts,
        "source_references": source_references,
        "input_quality": input_quality,
        "report_day_policy": report_day_policy,
    }


def _apply_report_day_policy(
    evidence: tuple[InputQualityEvidence, ...],
    policy: ReportDayPolicyResult,
) -> tuple[InputQualityEvidence, ...]:
    required = frozenset(policy.required_input_ids)
    closed_market_inputs = {
        region.market_input_id: region
        for region in policy.regions
        if not region.market_input_required
    }
    adjusted: list[InputQualityEvidence] = []
    observed_input_ids: set[str] = set()
    for item in evidence:
        observed_input_ids.add(item.input_id)
        if item.input_id in required:
            adjusted.append(replace(item, required=True))
            continue
        closed = closed_market_inputs.get(item.input_id)
        if closed is None:
            adjusted.append(item)
            continue
        if item.status in {"available", "revised"}:
            adjusted.append(replace(item, required=False))
            continue
        adjusted.append(
            replace(
                item,
                status="unavailable",
                required=False,
                reason=_closed_market_reason(closed.region, closed.status),
                facts=(),
                source_references=(),
            )
        )
    for input_id, closed in sorted(closed_market_inputs.items()):
        if input_id not in observed_input_ids:
            adjusted.append(
                InputQualityEvidence(
                    input_id=input_id,
                    status="unavailable",
                    required=False,
                    reason=_closed_market_reason(closed.region, closed.status),
                )
            )
    return tuple(adjusted)


def _closed_market_reason(region: str, status: str) -> str:
    return (
        f"{region} market is closed ({status}); "
        "current-session market input is optional for this report date"
    )


def _exclude_after_cutoff_evidence(
    evidence: InputQualityEvidence,
    *,
    cutoff_at: datetime,
) -> InputQualityEvidence:
    """Fail closed if an evidence adapter attempts to materialize late facts."""

    if evidence.status not in {"available", "revised"}:
        return evidence
    for fact in evidence.facts:
        available_at = _fact_available_at(fact)
        if available_at is None:
            return replace(
                evidence,
                status="invalid",
                reason="materialized fact is missing a valid available_at audit timestamp",
                facts=(),
                source_references=(),
            )
        if available_at > cutoff_at:
            return replace(
                evidence,
                status="late",
                reason="materialized fact became available after the report cutoff",
                facts=(),
                source_references=(),
            )
    return evidence


def _fact_available_at(fact: dict[str, object]) -> datetime | None:
    raw = fact.get("available_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return None if parsed.tzinfo is None else parsed.astimezone(UTC)


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "InputQualityEvidence",
    "ExchangeMarketSessionCalendar",
    "MarketSessionCalendar",
    "MaterializedReportInput",
    "PostgresReportInputEvidenceStore",
    "PostgresReportInputSnapshotStore",
    "ReportInputEvidenceStore",
    "ReportInputSnapshotMaterializer",
    "_unexpected_trading_dates",
]
