from __future__ import annotations

from datetime import date, time
from typing import Any, Literal

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from macro_platform.contracts.common import StrictModel

ReportStatus = Literal["complete", "degraded", "incomplete"]
ReportSectionStatus = Literal["complete", "degraded", "incomplete", "unavailable"]
PublicationDecision = Literal["published", "not_published"]

SECTION_LIMITS: dict[str, int] = {
    "executive_summary": 800,
    "cn_highlights": 1000,
    "hk_highlights": 1000,
    "us_highlights": 1000,
    "key_movements": 1200,
    "upcoming_calendar": 1600,
    "data_quality_notice": 600,
    "source_references": 4000,
}
REQUIRED_SECTION_IDS = frozenset(SECTION_LIMITS)


class ReportFreshness(StrictModel):
    market_close_max_age_hours: int = Field(ge=0)
    official_news_max_age_hours: int = Field(ge=0)
    macro_observation_max_age_days: int = Field(ge=0)


class ReportSchedule(StrictModel):
    publish_time_local: time
    late_data_cutoff_local: time
    run_policy: str = Field(min_length=1)
    holiday_policy: str = Field(min_length=1)
    calendar_id: str = Field(min_length=1)
    calendar_lookahead_days: int = Field(ge=0)
    freshness: ReportFreshness


class ReportCalendar(StrictModel):
    day_type: Literal["business_day", "holiday", "weekend"]
    holiday_notice: str | None = None


class ReportInputSnapshotRef(StrictModel):
    snapshot_id: str = Field(min_length=1)
    snapshot_version: str = Field(min_length=1)
    as_of: AwareDatetime
    cutoff_at: AwareDatetime
    fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    fact_ids: list[str] = Field(default_factory=list)


class ReportPublication(StrictModel):
    decision: PublicationDecision
    reason_code: str | None = None
    scheduled_publish_at: AwareDatetime
    published_at: AwareDatetime | None = None


class ReportQualityIssue(StrictModel):
    input_id: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ReportValidationIssue(StrictModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    severity: Literal["error", "warning"] = "error"
    input_id: str | None = None
    fact_id: str | None = None
    source_ref_id: str | None = None


class ReportDataQuality(StrictModel):
    status: ReportStatus
    missing_required_inputs: list[ReportQualityIssue] = Field(default_factory=list)
    stale_inputs: list[ReportQualityIssue] = Field(default_factory=list)
    late_inputs: list[ReportQualityIssue] = Field(default_factory=list)
    revised_inputs: list[ReportQualityIssue] = Field(default_factory=list)
    unavailable_inputs: list[ReportQualityIssue] = Field(default_factory=list)


class ReportSourceReference(StrictModel):
    source_ref_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=2)
    provider_record_id: str = Field(min_length=1)
    source_name: str = Field(min_length=1)
    source_url: HttpUrl | None = None
    retrieved_at: AwareDatetime
    checksum_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    external_llm_allowed: bool | None = None


class ReportClaim(StrictModel):
    """Machine-readable claim that must agree with its approved input fact."""

    claim_type: Literal["number", "date", "direction", "text"]
    fact_id: str = Field(min_length=1)
    value: Any
    unit: str | None = None
    direction: str | None = None
    period_start: date | None = None
    period_end: date | None = None


class ReportSection(StrictModel):
    section_id: str = Field(min_length=1)
    status: ReportSectionStatus
    character_count: int = Field(ge=0)
    max_characters: int = Field(ge=1)
    text: str | None = None
    fact_ids: list[str] = Field(default_factory=list)
    source_ref_ids: list[str] = Field(default_factory=list)
    items: list[dict[str, Any]] = Field(default_factory=list)
    claims: list[ReportClaim] = Field(default_factory=list)
    lookahead_days: int | None = Field(default=None, ge=0)
    issue_codes: list[str] = Field(default_factory=list)
    reason_code: str | None = None

    @model_validator(mode="after")
    def validate_visible_text(self) -> ReportSection:
        visible = self.text or ""
        for item in self.items:
            for field_name in ("label", "text", "name"):
                value = item.get(field_name)
                if isinstance(value, str):
                    visible += value
        actual_count = len(visible)
        if actual_count != self.character_count:
            raise ValueError(
                f"section {self.section_id} character_count is {self.character_count}, "
                f"expected {actual_count}"
            )
        limit = SECTION_LIMITS.get(self.section_id)
        if limit is not None and self.max_characters > limit:
            raise ValueError(
                f"section {self.section_id} max_characters exceeds the contract limit {limit}"
            )
        if self.character_count > self.max_characters:
            raise ValueError(f"section {self.section_id} exceeds max_characters")
        return self


class DailyReport(StrictModel):
    """Typed v1 public report contract produced from an approved input snapshot."""

    contract_name: Literal["DailyReport"]
    contract_version: Literal["1.0"]
    report_id: str = Field(min_length=1)
    report_date: date
    timezone: Literal["Asia/Shanghai"]
    schedule: ReportSchedule
    calendar: ReportCalendar
    generated_at: AwareDatetime
    input_snapshot: ReportInputSnapshotRef
    status: ReportStatus
    publication: ReportPublication
    data_quality: ReportDataQuality
    sections: dict[str, ReportSection]

    @model_validator(mode="after")
    def validate_references(self) -> DailyReport:
        if set(self.sections) != REQUIRED_SECTION_IDS:
            missing = sorted(REQUIRED_SECTION_IDS - set(self.sections))
            extra = sorted(set(self.sections) - REQUIRED_SECTION_IDS)
            raise ValueError(f"report sections mismatch; missing={missing}, extra={extra}")
        if any(section.section_id != section_id for section_id, section in self.sections.items()):
            raise ValueError("report section keys must match section_id")
        if self.status == "incomplete" and self.publication.decision != "not_published":
            raise ValueError("incomplete reports cannot be published")

        fact_ids = set(self.input_snapshot.fact_ids)
        referenced_fact_ids = _nested_reference_ids(self.sections, "fact_ids")
        referenced_fact_ids.update(
            claim.fact_id for section in self.sections.values() for claim in section.claims
        )
        if unknown_facts := referenced_fact_ids - fact_ids:
            raise ValueError(f"report references unknown input facts: {sorted(unknown_facts)}")

        source_section = self.sections["source_references"]
        source_references = [
            ReportSourceReference.model_validate(item) for item in source_section.items
        ]
        source_ids = {source.source_ref_id for source in source_references}
        if len(source_ids) != len(source_references):
            raise ValueError("source references must be unique")
        referenced_source_ids = _nested_reference_ids(self.sections, "source_ref_ids")
        if unknown_sources := referenced_source_ids - source_ids:
            raise ValueError(f"report references unknown sources: {sorted(unknown_sources)}")
        return self


def _nested_reference_ids(value: Any, field_name: str) -> set[str]:
    """Collect top-level and item-level provenance references from report sections."""

    reference_ids: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, ReportSection):
            visit(item.model_dump(mode="python"))
        elif isinstance(item, dict):
            references = item.get(field_name)
            if references is not None:
                if not isinstance(references, list) or not all(
                    isinstance(reference, str) for reference in references
                ):
                    raise ValueError(f"report {field_name} must be a list of strings")
                reference_ids.update(references)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return reference_ids
