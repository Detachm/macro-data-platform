"""Shared report-input quality decisions for workers and report validation.

This module evaluates completeness, freshness, quarantine, revision and
transient errors only.  Under ADR 0005, legacy source-rights markers are
provenance metadata and never influence a runtime quality decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from macro_platform.storage.reporting import ReportInputSnapshot

QualityGateStatus = Literal["passed", "degraded", "blocked", "retryable"]

REQUIRED_REPORT_INPUT_IDS = frozenset(
    {
        "market.cn.core_indices.previous_close",
        "news.cn.official_headlines_24h",
        "market.hk.core_indices.previous_close",
        "news.hk.official_headlines_24h",
        "market.us.core_indices.previous_close",
        "calendar.macro_releases_7d",
    }
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

        for input_id in sorted(REQUIRED_REPORT_INPUT_IDS - set(quality)):
            issues.append(
                QualityGateIssue(
                    input_id=input_id,
                    code="REQUIRED_INPUT_UNAVAILABLE",
                    message="required report input has no quality-gate result",
                    required=True,
                )
            )

        for input_id in sorted(key for key in quality if isinstance(key, str)):
            raw = quality[input_id]
            required = _is_required(input_id, raw)
            if (
                input_id in REQUIRED_REPORT_INPUT_IDS
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


def _is_required(input_id: str, raw: Any) -> bool:
    if input_id in REQUIRED_REPORT_INPUT_IDS:
        return True
    return not isinstance(raw, dict) or raw.get("required", False) is True


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
    return (
        bool(snapshot.fact_ids)
        and isinstance(snapshot.payload.get("facts"), list)
        and bool(snapshot.payload["facts"])
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
