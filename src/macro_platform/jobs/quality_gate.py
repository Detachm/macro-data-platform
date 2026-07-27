"""Report-input quality decisions used by the scheduled ingestion worker.

This module intentionally evaluates data quality only.  Source rights remain
provenance metadata under ADR 0005 and are not a runtime admission condition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from macro_platform.services.report_validation import REQUIRED_REPORT_INPUT_IDS
from macro_platform.storage.reporting import ReportInputSnapshot

QualityGateStatus = Literal["passed", "degraded", "blocked", "retryable"]

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
    """Turn persisted completeness, freshness and quarantine facts into a decision."""

    def evaluate(self, snapshot: ReportInputSnapshot) -> QualityGateResult:
        raw_quality = snapshot.payload.get("input_quality", {})
        quality = raw_quality if isinstance(raw_quality, dict) else {}
        issues: list[QualityGateIssue] = []

        for input_id in sorted(REQUIRED_REPORT_INPUT_IDS - set(quality)):
            issues.append(
                QualityGateIssue(
                    input_id=input_id,
                    code="MISSING_REQUIRED_INPUT",
                    message="required report input has no quality-gate result",
                    required=True,
                )
            )

        for input_id in sorted(quality):
            raw = quality[input_id]
            if not isinstance(input_id, str):
                continue
            required = _is_required(input_id, raw)
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
            reason = str(raw.get("reason", f"input status is {status}"))
            issue = _issue_for_status(
                input_id=input_id,
                status=status,
                reason=reason,
                required=required,
            )
            if issue is not None:
                issues.append(issue)

        return QualityGateResult(status=_decision(issues), issues=tuple(issues))


def _is_required(input_id: str, raw: Any) -> bool:
    if input_id in REQUIRED_REPORT_INPUT_IDS:
        return True
    return not isinstance(raw, dict) or raw.get("required", False) is True


def _issue_for_status(
    *, input_id: str, status: object, reason: str, required: bool
) -> QualityGateIssue | None:
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


def _decision(issues: list[QualityGateIssue]) -> QualityGateStatus:
    if any(issue.required and not issue.code.startswith("RETRYABLE_") for issue in issues):
        return "blocked"
    if any(issue.required and issue.code.startswith("RETRYABLE_") for issue in issues):
        return "retryable"
    if issues:
        return "degraded"
    return "passed"
