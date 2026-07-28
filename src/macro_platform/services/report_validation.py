from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from macro_platform.contracts.report import (
    DailyReport,
    ReportSourceReference,
    ReportStatus,
    ReportValidationIssue,
)
from macro_platform.services.report_day_policy import report_calendar_payload
from macro_platform.services.report_input_quality import (
    QualityGateIssue,
    ReportInputQualityGate,
)
from macro_platform.storage.reporting import ReportInputSnapshot, StoredDailyReport


@dataclass(frozen=True)
class _Fact:
    fact_id: str
    value: Any
    unit: str | None
    direction: str | None
    available_at: datetime | None
    report_date: date | None
    period_start: date | None
    period_end: date | None
    source_ref_ids: frozenset[str]
    invalid_available_at: bool
    invalid_report_date: bool


@dataclass(frozen=True)
class ReportValidationResult:
    report: StoredDailyReport
    issues: tuple[ReportValidationIssue, ...]

    @property
    def publishable(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)


class ReportValidationError(RuntimeError):
    pass


class ReportFallbackStore(Protocol):
    async def load_input_snapshot(self, snapshot_id: str) -> ReportInputSnapshot | None: ...

    async def put_report(self, report: StoredDailyReport) -> bool: ...


class ReportValidationStore(ReportFallbackStore, Protocol):
    async def update_report_validation(
        self, report: StoredDailyReport, *, expected_lifecycle_status: str
    ) -> bool: ...


class ReportValidator:
    """Validate generated claims against the persisted, point-in-time snapshot."""

    def validate(
        self,
        report: StoredDailyReport,
        snapshot: ReportInputSnapshot,
    ) -> ReportValidationResult:
        issues: list[ReportValidationIssue] = []
        try:
            typed_report = DailyReport.model_validate(report.payload)
        except ValidationError as error:
            issues.append(
                ReportValidationIssue(
                    code="REPORT_CONTRACT_INVALID",
                    message=str(error),
                )
            )
            return ReportValidationResult(report=report, issues=tuple(issues))

        if report.report_date != snapshot.report_date:
            issues.append(
                ReportValidationIssue(
                    code="REPORT_DATE_MISMATCH",
                    message="report date does not match the persisted input snapshot",
                    input_id=snapshot.snapshot_id,
                )
            )
        if report.input_snapshot_id != snapshot.snapshot_id:
            issues.append(
                ReportValidationIssue(
                    code="SNAPSHOT_ID_MISMATCH",
                    message="report references a different input snapshot",
                    input_id=snapshot.snapshot_id,
                )
            )
        snapshot_ref = typed_report.input_snapshot
        identity_mismatches: list[str] = []
        if snapshot_ref.snapshot_id != snapshot.snapshot_id:
            identity_mismatches.append("snapshot_id")
        if snapshot_ref.snapshot_version != snapshot.snapshot_version:
            identity_mismatches.append("snapshot_version")
        if snapshot_ref.as_of != snapshot.as_of:
            identity_mismatches.append("as_of")
        if snapshot_ref.cutoff_at != snapshot.cutoff_at:
            identity_mismatches.append("cutoff_at")
        if snapshot_ref.fingerprint_sha256 != snapshot.fingerprint_sha256:
            identity_mismatches.append("fingerprint_sha256")
        if snapshot_ref.fact_ids != snapshot.fact_ids:
            identity_mismatches.append("fact_ids")
        if identity_mismatches:
            issues.append(
                ReportValidationIssue(
                    code="SNAPSHOT_IDENTITY_MISMATCH",
                    message=(
                        "report input_snapshot differs from the persisted snapshot: "
                        + ", ".join(identity_mismatches)
                    ),
                    input_id=snapshot.snapshot_id,
                )
            )
        if typed_report.status == "incomplete":
            issues.append(
                ReportValidationIssue(
                    code="REPORT_INCOMPLETE",
                    message="incomplete reports cannot reach publishable state",
                )
            )

        facts = _load_facts(snapshot)
        source_records = _load_source_records(snapshot)
        snapshot_source_ids = frozenset(source_records)
        issues.extend(_quality_issues(snapshot))

        for source in _report_source_references(typed_report):
            snapshot_source = source_records.get(source.source_ref_id)
            if snapshot_source is None:
                issues.append(
                    ReportValidationIssue(
                        code="SOURCE_NOT_IN_SNAPSHOT",
                        message="source record is not present in the approved input snapshot",
                        source_ref_id=source.source_ref_id,
                    )
                )
            elif _source_record(source) != snapshot_source:
                issues.append(
                    ReportValidationIssue(
                        code="SOURCE_RECORD_MISMATCH",
                        message="report source metadata differs from the approved snapshot",
                        source_ref_id=source.source_ref_id,
                    )
                )

        checked_source_ids: set[str] = set()
        for section in typed_report.sections.values():
            section_source_ids = _section_reference_ids(section, "source_ref_ids")
            _append_source_issues(
                issues,
                section_source_ids,
                snapshot_source_ids,
                checked_source_ids,
            )

            section_fact_ids = _section_reference_ids(section, "fact_ids")
            section_fact_ids.update(claim.fact_id for claim in section.claims)
            for fact_id in section_fact_ids:
                fact = facts.get(fact_id)
                if fact is None:
                    issues.append(
                        ReportValidationIssue(
                            code="FACT_NOT_MATERIALIZED",
                            message=(
                                "fact ID is declared but its approved value is absent "
                                "from the snapshot"
                            ),
                            fact_id=fact_id,
                        )
                    )
                    continue
                _append_source_issues(
                    issues,
                    fact.source_ref_ids,
                    snapshot_source_ids,
                    checked_source_ids,
                )
                if fact.invalid_available_at:
                    issues.append(
                        ReportValidationIssue(
                            code="FACT_AVAILABLE_AT_INVALID",
                            message="fact available_at must be a timezone-aware timestamp",
                            fact_id=fact_id,
                        )
                    )
                if fact.invalid_report_date:
                    issues.append(
                        ReportValidationIssue(
                            code="FACT_REPORT_DATE_INVALID",
                            message="fact report_date must be an ISO date",
                            fact_id=fact_id,
                        )
                    )
                if fact.available_at is not None and fact.available_at > snapshot.cutoff_at:
                    issues.append(
                        ReportValidationIssue(
                            code="FACT_AFTER_CUTOFF",
                            message="fact became available after the snapshot cutoff",
                            fact_id=fact_id,
                        )
                    )
                if fact.report_date is not None and fact.report_date != snapshot.report_date:
                    issues.append(
                        ReportValidationIssue(
                            code="FACT_DATE_MISMATCH",
                            message="fact report date does not match the report date",
                            fact_id=fact_id,
                        )
                    )
                if not fact.source_ref_ids or not section_source_ids.intersection(
                    fact.source_ref_ids
                ):
                    issues.append(
                        ReportValidationIssue(
                            code="FACT_UNCITED",
                            message="reported fact has no matching cited source reference",
                            fact_id=fact_id,
                        )
                    )

            for claim in section.claims:
                fact = facts.get(claim.fact_id)
                if fact is None:
                    issues.append(
                        ReportValidationIssue(
                            code="CLAIM_FACT_NOT_FOUND",
                            message="machine-readable claim points to an unknown fact",
                            fact_id=claim.fact_id,
                        )
                    )
                    continue
                if not _values_equal(claim.value, fact.value):
                    issues.append(
                        ReportValidationIssue(
                            code="FACT_VALUE_MISMATCH",
                            message="machine-readable claim value differs from the approved fact",
                            fact_id=claim.fact_id,
                        )
                    )
                if claim.claim_type == "number" and claim.unit != fact.unit:
                    issues.append(
                        ReportValidationIssue(
                            code="FACT_UNIT_MISMATCH",
                            message="machine-readable claim unit differs from the approved fact",
                            fact_id=claim.fact_id,
                        )
                    )
                if claim.claim_type == "direction" and claim.direction != fact.direction:
                    issues.append(
                        ReportValidationIssue(
                            code="FACT_DIRECTION_MISMATCH",
                            message=(
                                "machine-readable claim direction differs from the approved fact"
                            ),
                            fact_id=claim.fact_id,
                        )
                    )
                if (claim.period_start, claim.period_end) != (
                    fact.period_start,
                    fact.period_end,
                ):
                    issues.append(
                        ReportValidationIssue(
                            code="FACT_PERIOD_MISMATCH",
                            message=(
                                "machine-readable claim period differs from the approved fact"
                            ),
                            fact_id=claim.fact_id,
                        )
                    )

        return ReportValidationResult(report=report, issues=tuple(issues))


@dataclass(frozen=True)
class ReportValidationServiceResult:
    report: StoredDailyReport
    issues: tuple[ReportValidationIssue, ...]
    used_fallback: bool


class ReportValidationService:
    """Apply validation state transitions and persist the operator audit trail."""

    def __init__(
        self,
        *,
        validator: ReportValidator | None = None,
        fallback_builder: ReportFallbackBuilder | None = None,
    ) -> None:
        self._validator = validator or ReportValidator()
        self._fallback_builder = fallback_builder or ReportFallbackBuilder()

    async def validate_or_fallback(
        self,
        store: ReportFallbackStore,
        *,
        snapshot_id: str,
        report_id: str,
        report_version: str,
        generated_at: datetime,
        candidate: StoredDailyReport | None,
        generation_id: UUID | None = None,
    ) -> ReportValidationServiceResult:
        snapshot = await store.load_input_snapshot(snapshot_id)
        if snapshot is None:
            raise ReportValidationError(f"input snapshot does not exist: {snapshot_id}")

        used_fallback = candidate is None
        report = candidate or self._fallback_builder.build(
            snapshot,
            report_id=report_id,
            report_version=report_version,
            generated_at=generated_at,
            generation_id=generation_id,
        )
        validation = self._validator.validate(report, snapshot)
        finalized = _finalize_report(report, validation)
        if used_fallback:
            if not await store.put_report(finalized):
                raise ReportValidationError("fallback report already exists")
        else:
            validation_store = cast(ReportValidationStore, store)
            if not await validation_store.update_report_validation(
                finalized,
                expected_lifecycle_status=report.lifecycle_status,
            ):
                raise ReportValidationError("report validation state was changed by another worker")
        return ReportValidationServiceResult(
            report=finalized,
            issues=validation.issues,
            used_fallback=used_fallback,
        )


class ReportFallbackBuilder:
    """Build a report from approved fact display values without calling an LLM."""

    def build(
        self,
        snapshot: ReportInputSnapshot,
        *,
        report_id: str,
        report_version: str,
        generated_at: datetime,
        generation_id: UUID | None = None,
    ) -> StoredDailyReport:
        raw_facts = _raw_facts(snapshot)
        quality_issues = _quality_issues(snapshot)
        has_blocking_issue = any(issue.severity == "error" for issue in quality_issues)
        status: ReportStatus = (
            "incomplete" if has_blocking_issue else ("degraded" if quality_issues else "complete")
        )
        publication_decision: Literal["published", "not_published"] = (
            "not_published" if has_blocking_issue else "published"
        )
        sections: dict[str, dict[str, Any]] = {}
        for section_id, max_characters in {
            "executive_summary": 800,
            "cn_highlights": 1000,
            "hk_highlights": 1000,
            "us_highlights": 1000,
            "key_movements": 1200,
            "upcoming_calendar": 1600,
            "data_quality_notice": 600,
            "source_references": 4000,
        }.items():
            if section_id == "source_references":
                items = [_source_item(raw) for raw in _raw_sources(snapshot)]
                sections[section_id] = _section(
                    section_id,
                    status="complete" if items else "unavailable",
                    max_characters=max_characters,
                    items=items,
                )
                continue
            if section_id == "data_quality_notice":
                text = _quality_notice(status, quality_issues)
                sections[section_id] = _section(
                    section_id,
                    status="complete",
                    max_characters=max_characters,
                    text=text,
                    issue_codes=[issue.code for issue in quality_issues],
                )
                continue

            section_facts = [raw for raw in raw_facts if raw.get("section_id") == section_id]
            items = [_fact_item(raw) for raw in section_facts]
            claims = [_fact_claim(raw) for raw in section_facts if raw.get("value") is not None]
            if section_id == "upcoming_calendar" and not items:
                section = _section(
                    section_id,
                    status="complete" if not has_blocking_issue else "unavailable",
                    max_characters=max_characters,
                    items=[],
                    lookahead_days=7,
                )
            elif section_id == "key_movements" and not items:
                section = _section(
                    section_id,
                    status="incomplete" if has_blocking_issue else "complete",
                    max_characters=max_characters,
                    items=[],
                    reason_code="REQUIRED_INPUT_UNAVAILABLE"
                    if has_blocking_issue
                    else "NO_VERIFIED_MOVEMENTS",
                )
            elif items:
                section = _section(
                    section_id,
                    status="incomplete" if has_blocking_issue else "complete",
                    max_characters=max_characters,
                    items=items,
                    claims=claims,
                )
            else:
                section = _section(
                    section_id,
                    status="unavailable" if has_blocking_issue else "degraded",
                    max_characters=max_characters,
                    text="数据不可用。" if has_blocking_issue else "暂无已验证事实。",
                    reason_code="REQUIRED_INPUT_UNAVAILABLE"
                    if has_blocking_issue
                    else "NO_VERIFIED_FACTS",
                )
            sections[section_id] = section

        payload = {
            "contract_name": "DailyReport",
            "contract_version": "1.0",
            "report_id": report_id,
            "report_date": snapshot.report_date.isoformat(),
            "timezone": "Asia/Shanghai",
            "schedule": _default_schedule(),
            "calendar": report_calendar_payload(
                snapshot.payload.get("report_day_policy"), report_date=snapshot.report_date
            ),
            "generated_at": generated_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "input_snapshot": {
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_version": snapshot.snapshot_version,
                "as_of": snapshot.as_of.isoformat().replace("+00:00", "Z"),
                "cutoff_at": snapshot.cutoff_at.isoformat().replace("+00:00", "Z"),
                "fingerprint_sha256": snapshot.fingerprint_sha256,
                "fact_ids": snapshot.fact_ids,
            },
            "status": status,
            "publication": {
                "decision": publication_decision,
                "reason_code": "REQUIRED_INPUT_UNAVAILABLE" if has_blocking_issue else None,
                "scheduled_publish_at": _scheduled_publish_at(snapshot),
                "published_at": None,
            },
            "data_quality": _data_quality(quality_issues, status),
            "sections": sections,
        }
        return StoredDailyReport(
            report_id=report_id,
            report_date=snapshot.report_date,
            report_version=report_version,
            contract_version="1.0",
            input_snapshot_id=snapshot.snapshot_id,
            status=status,
            publication_decision=publication_decision,
            generated_at=generated_at.astimezone(UTC),
            payload=payload,
            lifecycle_status="generated",
            generation_id=generation_id,
        )


def _finalize_report(
    report: StoredDailyReport,
    validation: ReportValidationResult,
) -> StoredDailyReport:
    errors = [issue for issue in validation.issues if issue.severity == "error"]
    warnings = [issue for issue in validation.issues if issue.severity == "warning"]
    publishable = not errors and report.status != "incomplete"
    status = "incomplete" if not publishable else ("degraded" if warnings else report.status)
    payload = deepcopy(report.payload)
    payload["status"] = status
    publication = dict(payload.get("publication", {}))
    publication.update(
        {
            "decision": "published" if publishable else "not_published",
            "reason_code": None if publishable else "VALIDATION_FAILED",
        }
    )
    payload["publication"] = publication
    quality = dict(payload.get("data_quality", {}))
    quality["status"] = status
    for issue in validation.issues:
        entry = {
            "input_id": issue.input_id or issue.fact_id or issue.source_ref_id or issue.code,
            "reason_code": issue.code,
            "reason": issue.message,
        }
        bucket = _quality_bucket(issue.code)
        entries = list(quality.get(bucket, []))
        existing_keys = {
            (item.get("input_id"), item.get("reason_code"))
            for item in entries
            if isinstance(item, dict)
        }
        key = (entry["input_id"], entry["reason_code"])
        if key not in existing_keys:
            entries.append(entry)
        quality[bucket] = entries
    payload["data_quality"] = quality
    notice = dict(payload.get("sections", {}).get("data_quality_notice", {}))
    notice_text = _quality_notice(status, list(validation.issues))
    notice.update(
        {
            "status": "complete",
            "text": notice_text,
            "character_count": len(notice_text),
            "issue_codes": sorted({issue.code for issue in validation.issues}),
        }
    )
    payload.setdefault("sections", {})["data_quality_notice"] = notice
    return report.model_copy(
        update={
            "status": status,
            "publication_decision": publication["decision"],
            "payload": payload,
            "lifecycle_status": "validated" if publishable else "failed",
            "validation_errors": list(validation.issues),
        }
    )


def _load_facts(snapshot: ReportInputSnapshot) -> dict[str, _Fact]:
    raw_facts = _raw_facts(snapshot)
    if not isinstance(raw_facts, list):
        return {}
    facts: dict[str, _Fact] = {}
    for raw in raw_facts:
        if not isinstance(raw, dict) or not isinstance(raw.get("fact_id"), str):
            continue
        available_at = _parse_datetime(raw.get("available_at"))
        report_date = _parse_date(raw.get("report_date"))
        source_ref_ids = raw.get("source_ref_ids", [])
        facts[raw["fact_id"]] = _Fact(
            fact_id=raw["fact_id"],
            value=raw.get("value"),
            unit=raw.get("unit") if isinstance(raw.get("unit"), str) else None,
            direction=raw.get("direction") if isinstance(raw.get("direction"), str) else None,
            available_at=available_at,
            report_date=report_date,
            period_start=_parse_date(raw.get("period_start")),
            period_end=_parse_date(raw.get("period_end")),
            source_ref_ids=frozenset(ref_id for ref_id in source_ref_ids if isinstance(ref_id, str))
            if isinstance(source_ref_ids, list)
            else frozenset(),
            invalid_available_at=raw.get("available_at") is not None and available_at is None,
            invalid_report_date=raw.get("report_date") is not None and report_date is None,
        )
    return facts


def _raw_facts(snapshot: ReportInputSnapshot) -> list[dict[str, Any]]:
    raw_facts = snapshot.payload.get("facts", [])
    return (
        [raw for raw in raw_facts if isinstance(raw, dict)] if isinstance(raw_facts, list) else []
    )


def _raw_sources(snapshot: ReportInputSnapshot) -> list[dict[str, Any]]:
    raw_sources = snapshot.payload.get("source_references", [])
    return (
        [raw for raw in raw_sources if isinstance(raw, dict)]
        if isinstance(raw_sources, list)
        else []
    )


def _report_source_references(report: DailyReport) -> list[ReportSourceReference]:
    return [
        ReportSourceReference.model_validate(item)
        for item in report.sections["source_references"].items
    ]


def _load_source_records(snapshot: ReportInputSnapshot) -> dict[str, dict[str, Any]]:
    return {
        raw["source_ref_id"]: _source_record(raw)
        for raw in _raw_sources(snapshot)
        if isinstance(raw.get("source_ref_id"), str)
    }


def _source_record(source: ReportSourceReference | dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any]
    if isinstance(source, ReportSourceReference):
        values = source.model_dump(mode="json")
    else:
        values = {
            key: source.get(key)
            for key in (
                "source_ref_id",
                "provider_id",
                "provider_record_id",
                "source_name",
                "source_url",
                "retrieved_at",
                "checksum_sha256",
                "external_llm_allowed",
            )
        }
    return {
        key: value
        for key, value in values.items()
        if key != "external_llm_allowed" or value is not None
    }


def _fact_item(raw: dict[str, Any]) -> dict[str, Any]:
    fact_id = raw["fact_id"]
    display_text = str(raw.get("display_text", fact_id))
    source_ref_ids = [ref_id for ref_id in raw.get("source_ref_ids", []) if isinstance(ref_id, str)]
    return {
        "label": str(raw.get("label", fact_id)),
        "text": display_text,
        "fact_ids": [fact_id],
        "source_ref_ids": source_ref_ids,
    }


def _fact_claim(raw: dict[str, Any]) -> dict[str, Any]:
    claim_type = raw.get("claim_type", "text")
    return {
        "claim_type": claim_type
        if claim_type in {"number", "date", "direction", "text"}
        else "text",
        "fact_id": raw["fact_id"],
        "value": raw.get("value"),
        "unit": raw.get("unit"),
        "direction": raw.get("direction"),
        "period_start": raw.get("period_start"),
        "period_end": raw.get("period_end"),
    }


def _source_item(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        key: raw[key]
        for key in (
            "source_ref_id",
            "provider_id",
            "provider_record_id",
            "source_name",
            "source_url",
            "retrieved_at",
            "checksum_sha256",
            "external_llm_allowed",
        )
        if key in raw
    }


def _section(
    section_id: str,
    *,
    status: str,
    max_characters: int,
    text: str | None = None,
    items: list[dict[str, Any]] | None = None,
    claims: list[dict[str, Any]] | None = None,
    lookahead_days: int | None = None,
    issue_codes: list[str] | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    items = items or []
    if text is not None:
        visible = text
    else:
        visible = "".join(
            str(item.get(field, "")) for item in items for field in ("label", "text", "name")
        )
    fact_ids = {fact_id for item in items for fact_id in _nested_reference_ids(item, "fact_ids")}
    source_ref_ids = {
        source_ref_id
        for item in items
        for source_ref_id in _nested_reference_ids(item, "source_ref_ids")
    }
    result: dict[str, Any] = {
        "section_id": section_id,
        "status": status,
        "character_count": len(visible),
        "max_characters": max_characters,
        "items": items,
    }
    if fact_ids:
        result["fact_ids"] = sorted(fact_ids)
    if source_ref_ids:
        result["source_ref_ids"] = sorted(source_ref_ids)
    if text is not None:
        result["text"] = text
    if claims:
        result["claims"] = claims
    if lookahead_days is not None:
        result["lookahead_days"] = lookahead_days
    if issue_codes:
        result["issue_codes"] = issue_codes
    if reason_code is not None:
        result["reason_code"] = reason_code
    return result


def _section_reference_ids(section: Any, field_name: str) -> set[str]:
    reference_ids = set(getattr(section, field_name, []))
    reference_ids.update(
        reference_id
        for item in getattr(section, "items", [])
        for reference_id in _nested_reference_ids(item, field_name)
    )
    return reference_ids


def _nested_reference_ids(value: Any, field_name: str) -> set[str]:
    reference_ids: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            references = item.get(field_name)
            if isinstance(references, list):
                reference_ids.update(
                    reference for reference in references if isinstance(reference, str)
                )
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return reference_ids


def _append_source_issues(
    issues: list[ReportValidationIssue],
    source_ref_ids: set[str] | frozenset[str],
    snapshot_source_ids: frozenset[str],
    checked_source_ids: set[str],
) -> None:
    for source_ref_id in sorted(source_ref_ids - checked_source_ids):
        checked_source_ids.add(source_ref_id)
        if source_ref_id not in snapshot_source_ids:
            issues.append(
                ReportValidationIssue(
                    code="SOURCE_NOT_IN_SNAPSHOT",
                    message="source reference is not present in the approved input snapshot",
                    source_ref_id=source_ref_id,
                )
            )


def _quality_notice(status: str, issues: list[ReportValidationIssue]) -> str:
    if not issues:
        return "必需数据均已通过质量校验。"
    if status == "incomplete":
        return "日报未发布：必需数据未满足质量校验。"
    return "部分可选数据不可用，日报已降级。"


def _data_quality(issues: list[ReportValidationIssue], status: str) -> dict[str, Any]:
    entries = [
        {
            "input_id": issue.input_id or issue.code,
            "reason_code": issue.code,
            "reason": issue.message,
        }
        for issue in issues
    ]
    return {
        "status": status,
        "missing_required_inputs": [
            entry for entry in entries if entry["reason_code"] == "REQUIRED_INPUT_UNAVAILABLE"
        ],
        "stale_inputs": [entry for entry in entries if "STALE" in entry["reason_code"]],
        "late_inputs": [entry for entry in entries if "LATE" in entry["reason_code"]],
        "revised_inputs": [entry for entry in entries if "REVISED" in entry["reason_code"]],
        "unavailable_inputs": [
            entry
            for entry in entries
            if entry["reason_code"] not in {"REQUIRED_INPUT_UNAVAILABLE"}
            and entry["reason_code"]
            not in {
                "STALE_REQUIRED_INPUT",
                "STALE_OPTIONAL_INPUT",
                "LATE_REQUIRED_INPUT",
                "LATE_OPTIONAL_INPUT",
                "REVISED_REQUIRED_INPUT",
                "REVISED_OPTIONAL_INPUT",
            }
        ],
    }


def _quality_bucket(code: str) -> str:
    if code == "REQUIRED_INPUT_UNAVAILABLE":
        return "missing_required_inputs"
    if "STALE" in code:
        return "stale_inputs"
    if "LATE" in code:
        return "late_inputs"
    if "REVISED" in code:
        return "revised_inputs"
    return "unavailable_inputs"


def _default_schedule() -> dict[str, Any]:
    return {
        "publish_time_local": "08:30:00",
        "late_data_cutoff_local": "08:15:00",
        "run_policy": "every_calendar_day",
        "holiday_policy": "publish_with_notice",
        "calendar_id": "cn_hk_report_calendar_v1",
        "calendar_lookahead_days": 7,
        "freshness": {
            "market_close_max_age_hours": 36,
            "official_news_max_age_hours": 24,
            "macro_observation_max_age_days": 45,
        },
    }


def _scheduled_publish_at(snapshot: ReportInputSnapshot) -> str:
    local = datetime.combine(
        snapshot.report_date,
        time(8, 30),
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    return local.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _quality_issues(snapshot: ReportInputSnapshot) -> list[ReportValidationIssue]:
    return [
        _validation_issue(issue) for issue in ReportInputQualityGate().evaluate(snapshot).issues
    ]


def _validation_issue(issue: QualityGateIssue) -> ReportValidationIssue:
    return ReportValidationIssue(
        code=issue.code,
        message=issue.message,
        severity=(
            "warning" if not issue.required or issue.code.startswith("REVISED_") else "error"
        ),
        input_id=issue.input_id,
    )


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    except ValueError:
        return None


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, (int, float, Decimal)) and isinstance(right, (int, float, Decimal, str)):
        try:
            return Decimal(str(left)) == Decimal(str(right))
        except InvalidOperation:
            return False
    return bool(left == right)
