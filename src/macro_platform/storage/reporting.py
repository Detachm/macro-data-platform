from __future__ import annotations

from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from macro_platform.contracts.common import StrictModel

ReportLifecycleStatus = Literal["draft", "generated", "failed", "validated", "superseded"]


class ReportInputSnapshot(StrictModel):
    """Immutable, point-in-time input set used to generate a DailyReport version."""

    snapshot_id: str = Field(min_length=1, max_length=128)
    snapshot_version: str = Field(min_length=1, max_length=32)
    report_date: date
    as_of: AwareDatetime
    cutoff_at: AwareDatetime
    fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    fact_ids: list[str] = Field(default_factory=list)
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_payload_contract(self) -> ReportInputSnapshot:
        expected = {
            "snapshot_id": self.snapshot_id,
            "snapshot_version": self.snapshot_version,
            "as_of": self.as_of.isoformat().replace("+00:00", "Z"),
            "cutoff_at": self.cutoff_at.isoformat().replace("+00:00", "Z"),
            "fingerprint_sha256": self.fingerprint_sha256,
            "fact_ids": self.fact_ids,
        }
        identity = {key: self.payload.get(key) for key in expected}
        if identity != expected:
            raise ValueError("snapshot payload must match storage identity and input facts")
        return self


class DailyReportSourceRef(StrictModel):
    source_ref_id: str = Field(min_length=1, max_length=128)
    provider_id: str = Field(min_length=2, max_length=64)
    provider_record_id: str = Field(min_length=1, max_length=256)
    source_name: str = Field(min_length=1, max_length=256)
    source_url: str | None = None
    retrieved_at: AwareDatetime
    checksum_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    external_llm_allowed: bool | None = None


class StoredDailyReport(StrictModel):
    """Storage command for one immutable DailyReport version.

    ``report_version`` is a persistence identity (for example ``v1``), kept
    separate from the public ``contract_version``.  Regeneration therefore
    creates a new immutable report instead of overwriting the prior version.
    """

    report_id: str = Field(min_length=1, max_length=128)
    report_date: date
    report_version: str = Field(min_length=1, max_length=64)
    contract_version: str = Field(min_length=1, max_length=32)
    input_snapshot_id: str = Field(min_length=1, max_length=128)
    status: Literal["complete", "degraded", "incomplete"]
    publication_decision: Literal["published", "not_published"]
    generated_at: AwareDatetime
    payload: dict[str, Any]
    lifecycle_status: ReportLifecycleStatus = "generated"
    generation_id: UUID | None = None

    @model_validator(mode="after")
    def validate_payload_identity(self) -> StoredDailyReport:
        if self.payload.get("report_id") != self.report_id:
            raise ValueError("report payload report_id must match storage identity")
        if self.payload.get("report_date") != self.report_date.isoformat():
            raise ValueError("report payload report_date must match storage identity")
        if self.payload.get("contract_version") != self.contract_version:
            raise ValueError("report payload contract_version must match storage identity")
        if self.payload.get("status") != self.status:
            raise ValueError("report payload status must match storage identity")
        if self.payload.get("publication", {}).get("decision") != self.publication_decision:
            raise ValueError("report payload publication decision must match storage identity")
        if self.payload.get("input_snapshot", {}).get("snapshot_id") != self.input_snapshot_id:
            raise ValueError("report payload snapshot_id must match storage identity")
        source_references = self.source_references()
        source_reference_ids = {source.source_ref_id for source in source_references}
        if len(source_reference_ids) != len(source_references):
            raise ValueError(
                "report payload source_references must use unique source_ref_id values"
            )
        unknown_source_reference_ids = (
            self._section_reference_ids("source_ref_ids") - source_reference_ids
        )
        if unknown_source_reference_ids:
            raise ValueError(
                "report payload source_ref_ids must resolve to source_references: "
                f"{sorted(unknown_source_reference_ids)}"
            )
        return self

    def source_references(self) -> list[DailyReportSourceRef]:
        section = self.payload.get("sections", {}).get("source_references", {})
        items = section.get("items") if isinstance(section, dict) else None
        if not isinstance(items, list):
            raise ValueError("report payload source_references.items is required")
        return [DailyReportSourceRef.model_validate(item) for item in items]

    def validate_fact_references(self, available_fact_ids: list[str]) -> None:
        """Ensure every section fact comes from the immutable input snapshot."""

        unknown_fact_ids = self._section_reference_ids("fact_ids") - set(available_fact_ids)
        if unknown_fact_ids:
            raise ValueError(
                "report payload fact_ids must resolve to input snapshot facts: "
                f"{sorted(unknown_fact_ids)}"
            )

    def _section_reference_ids(self, field_name: str) -> set[str]:
        sections = self.payload.get("sections")
        if not isinstance(sections, dict):
            raise ValueError("report payload sections is required")

        reference_ids: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                references = value.get(field_name)
                if references is not None:
                    if not isinstance(references, list) or not all(
                        isinstance(reference, str) for reference in references
                    ):
                        raise ValueError(f"report payload {field_name} must be a list of strings")
                    reference_ids.update(references)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(sections)
        return reference_ids

class ReportGenerationAttempt(StrictModel):
    """Auditable state and model trace for one generation attempt."""

    generation_id: UUID
    report_id: str = Field(min_length=1, max_length=128)
    report_version: str = Field(min_length=1, max_length=64)
    input_snapshot_id: str = Field(min_length=1, max_length=128)
    lifecycle_status: ReportLifecycleStatus = "draft"
    attempt_no: int = Field(default=1, ge=1)
    prompt_version: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    model_parameters: dict[str, Any] = Field(default_factory=dict)
    input_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_ref_ids: list[str] = Field(default_factory=list)
    error_code: str | None = None
    response_payload: dict[str, Any] | None = None

class DeliveryAttempt(StrictModel):
    """One idempotent delivery record; retries update this record, not the report."""

    delivery_id: UUID
    report_id: str = Field(min_length=1, max_length=128)
    delivery_target: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)
    attempt_no: int = Field(default=1, ge=1)
    status: Literal["pending", "succeeded", "failed", "retry_wait"] = "pending"
    request_payload: dict[str, Any] = Field(default_factory=dict)
    response_payload: dict[str, Any] | None = None
