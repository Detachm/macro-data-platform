from __future__ import annotations

from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from macro_platform.contracts.common import StrictModel


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

    @model_validator(mode="after")
    def validate_payload_identity(self) -> StoredDailyReport:
        if self.payload.get("report_id") != self.report_id:
            raise ValueError("report payload report_id must match storage identity")
        if self.payload.get("input_snapshot", {}).get("snapshot_id") != self.input_snapshot_id:
            raise ValueError("report payload snapshot_id must match storage identity")
        return self


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
