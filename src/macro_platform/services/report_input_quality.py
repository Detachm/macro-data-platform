"""Shared report-input quality decisions for workers and report validation.

This module evaluates completeness, freshness, quarantine, revision and
transient errors only.  Under ADR 0005, legacy source-rights markers are
provenance metadata and never influence a runtime quality decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from macro_platform.services.report_day_policy import (
    ALWAYS_REQUIRED_REPORT_INPUT_IDS,
    MARKET_INPUT_ID_BY_REGION,
    REPORT_DAY_POLICY_ID,
)
from macro_platform.storage.reporting import ReportInputSnapshot

QualityGateStatus = Literal["passed", "degraded", "blocked", "retryable"]

REQUIRED_REPORT_INPUT_IDS = ALWAYS_REQUIRED_REPORT_INPUT_IDS | frozenset(
    MARKET_INPUT_ID_BY_REGION.values()
)

_BLOCKING_STATUSES = frozenset(
    {"missing", "stale", "late", "unavailable", "quarantined", "invalid"}
)
_KNOWN_STATUSES = _BLOCKING_STATUSES | {"available", "revised", "retryable"}


@dataclass(frozen=True, slots=True)
class QualityGateIssue:
    input_id: str
    code: str
    message: str
    required: bool


@dataclass(frozen=True, slots=True)
class QualityGateResult:
    status: QualityGateStatus
    issues: tuple[QualityGateIssue, ...]


class ReportInputQualityGate:
    """Turn one immutable input snapshot into a data-quality decision."""

    def evaluate(self, snapshot: ReportInputSnapshot) -> QualityGateResult:
        raw_quality = snapshot.payload.get("input_quality", {})
        quality = raw_quality if isinstance(raw_quality, dict) else {}
        issues: list[QualityGateIssue] = []
        required_input_ids, optional_input_ids, policy_issue = _inputs_for_snapshot(snapshot)
        if policy_issue is not None:
            issues.append(policy_issue)

        for input_id in sorted(required_input_ids - set(quality)):
            issues.append(
                QualityGateIssue(
                    input_id=input_id,
                    code="REQUIRED_INPUT_UNAVAILABLE",
                    message="required report input has no quality-gate result",
                    required=True,
                )
            )
        for input_id in sorted(optional_input_ids - set(quality)):
            issues.append(
                QualityGateIssue(
                    input_id=input_id,
                    code="OPTIONAL_INPUT_UNAVAILABLE",
                    message="closed-market input has no explicit quality-gate result",
                    required=False,
                )
            )
        for input_id in sorted(optional_input_ids & set(quality)):
            raw = quality[input_id]
            if isinstance(raw, dict) and raw.get("status") == "available":
                issues.append(
                    QualityGateIssue(
                        input_id=input_id,
                        code="SCHEDULED_MARKET_CLOSURE",
                        message="market is closed for this report date; latest facts are optional",
                        required=False,
                    )
                )

        for input_id in sorted(key for key in quality if isinstance(key, str)):
            raw = quality[input_id]
            required = _is_required(input_id, raw, required_input_ids=required_input_ids)
            if (
                input_id in required_input_ids
                and isinstance(raw, dict)
                and raw.get("required", True) is not True
            ):
                issues.append(
                    QualityGateIssue(
                        input_id=input_id,
                        code="REQUIRED_INPUT_DECLARATION_INVALID",
                        message="required report input cannot be downgraded to optional",
                        required=True,
                    )
                )
            if not isinstance(raw, dict):
                issues.append(
                    QualityGateIssue(
                        input_id=input_id,
                        code=(
                            "INVALID_REQUIRED_INPUT_QUALITY"
                            if required
                            else "INVALID_OPTIONAL_INPUT_QUALITY"
                        ),
                        message="input quality entry must be an object",
                        required=required,
                    )
                )
                continue
            status = raw.get("status")
            issue = _issue_for_status(
                input_id=input_id,
                status=status,
                reason=str(raw.get("reason", f"input status is {status}")),
                required=required,
            )
            if issue is not None:
                issues.append(issue)

        if not _has_materialized_facts(snapshot):
            issues.append(
                QualityGateIssue(
                    input_id="report.facts",
                    code="REQUIRED_FACTS_UNAVAILABLE",
                    message="approved input snapshot contains no materialized report facts",
                    required=True,
                )
            )
        return QualityGateResult(status=_decision(issues), issues=tuple(issues))


def _is_required(
    input_id: str,
    raw: Any,
    *,
    required_input_ids: frozenset[str],
) -> bool:
    if input_id in required_input_ids:
        return True
    return not isinstance(raw, dict) or raw.get("required", False) is True


def _inputs_for_snapshot(
    snapshot: ReportInputSnapshot,
) -> tuple[frozenset[str], frozenset[str], QualityGateIssue | None]:
    raw = snapshot.payload.get("report_day_policy")
    if raw is None:
        return REQUIRED_REPORT_INPUT_IDS, frozenset(), None
    invalid = QualityGateIssue(
        input_id="report.day_policy",
        code="REPORT_DAY_POLICY_INVALID",
        message="report-day policy is incomplete or inconsistent with the input contract",
        required=True,
    )
    if not isinstance(raw, dict) or raw.get("report_date") != snapshot.report_date.isoformat():
        return REQUIRED_REPORT_INPUT_IDS, frozenset(), invalid
    if (
        raw.get("policy_id") != REPORT_DAY_POLICY_ID
        or not isinstance(raw.get("calendar_version"), str)
        or not raw.get("calendar_version")
    ):
        return REQUIRED_REPORT_INPUT_IDS, frozenset(), invalid
    raw_required = raw.get("required_input_ids")
    raw_optional = raw.get("optional_input_ids")
    if not isinstance(raw_required, (list, tuple)) or not isinstance(raw_optional, (list, tuple)):
        return REQUIRED_REPORT_INPUT_IDS, frozenset(), invalid
    if not all(isinstance(item, str) for item in (*raw_required, *raw_optional)):
        return REQUIRED_REPORT_INPUT_IDS, frozenset(), invalid
    required = frozenset(raw_required)
    optional = frozenset(raw_optional)
    if (
        required & optional
        or required | optional != REQUIRED_REPORT_INPUT_IDS
        or not required >= ALWAYS_REQUIRED_REPORT_INPUT_IDS
        or not _policy_regions_match(
            raw,
            report_date=snapshot.report_date,
            optional_input_ids=optional,
        )
    ):
        return REQUIRED_REPORT_INPUT_IDS, frozenset(), invalid
    return required, optional, None


def _policy_regions_match(
    raw_policy: dict[str, Any],
    *,
    report_date: date,
    optional_input_ids: frozenset[str],
) -> bool:
    day_type = raw_policy.get("day_type")
    regions = raw_policy.get("regions")
    if day_type not in {"regular", "weekend", "regional_holiday"} or not isinstance(
        regions, (list, tuple)
    ):
        return False
    expected_market_ids = {
        region.value: input_id for region, input_id in MARKET_INPUT_ID_BY_REGION.items()
    }
    observed_statuses: dict[str, object] = {}
    derived_optional: set[str] = set()
    for item in regions:
        if not isinstance(item, dict):
            return False
        region = item.get("region")
        if region not in expected_market_ids or region in observed_statuses:
            return False
        market_input_id = item.get("market_input_id")
        status = item.get("status")
        required = item.get("market_input_required")
        if (
            market_input_id != expected_market_ids[region]
            or status not in {"scheduled_session", "weekend_closed", "exchange_holiday"}
            or not isinstance(required, bool)
            or required != (status == "scheduled_session")
        ):
            return False
        observed_statuses[region] = status
        if not required:
            derived_optional.add(market_input_id)
    if set(observed_statuses) != set(expected_market_ids) or derived_optional != optional_input_ids:
        return False
    statuses = set(observed_statuses.values())
    weekday = report_date.weekday()
    return (
        (day_type == "regular" and weekday < 5 and statuses == {"scheduled_session"})
        or (day_type == "weekend" and weekday >= 5 and statuses == {"weekend_closed"})
        or (
            day_type == "regional_holiday"
            and weekday < 5
            and "exchange_holiday" in statuses
            and statuses <= {"scheduled_session", "exchange_holiday"}
        )
    )


def _issue_for_status(
    *, input_id: str, status: object, reason: str, required: bool
) -> QualityGateIssue | None:
    # Historical ``denied`` values were a source-rights policy artifact.  They
    # remain auditable in the snapshot but must not alter an internal workflow.
    if status == "denied":
        return None
    if not isinstance(status, str) or status not in _KNOWN_STATUSES:
        return QualityGateIssue(
            input_id=input_id,
            code="INVALID_REQUIRED_INPUT_STATUS" if required else "INVALID_INPUT_STATUS",
            message="input quality status is not recognized",
            required=required,
        )
    if status == "available":
        return None
    suffix = "REQUIRED_INPUT" if required else "OPTIONAL_INPUT"
    if status == "revised":
        return QualityGateIssue(input_id, f"REVISED_{suffix}", reason, required)
    if status == "retryable":
        return QualityGateIssue(input_id, f"RETRYABLE_{suffix}", reason, required)
    return QualityGateIssue(input_id, f"{status.upper()}_{suffix}", reason, required)


def _has_materialized_facts(snapshot: ReportInputSnapshot) -> bool:
    raw_facts = snapshot.payload.get("facts")
    if not snapshot.fact_ids or not isinstance(raw_facts, list) or not raw_facts:
        return False
    fact_ids = [fact.get("fact_id") for fact in raw_facts if isinstance(fact, dict)]
    return (
        len(snapshot.fact_ids) == len(set(snapshot.fact_ids))
        and len(fact_ids) == len(raw_facts) == len(snapshot.fact_ids)
        and set(fact_ids) == set(snapshot.fact_ids)
    )


def _decision(issues: list[QualityGateIssue]) -> QualityGateStatus:
    if any(
        issue.required and not issue.code.startswith(("RETRYABLE_", "REVISED_")) for issue in issues
    ):
        return "blocked"
    if any(issue.required and issue.code.startswith("RETRYABLE_") for issue in issues):
        return "retryable"
    if issues:
        return "degraded"
    return "passed"
