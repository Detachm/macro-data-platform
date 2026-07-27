from __future__ import annotations

from enum import StrEnum
from importlib.resources import files
from typing import Protocol

from pydantic import Field, model_validator

from macro_platform.contracts.common import Region, StrictModel
from macro_platform.contracts.provider import Dataset


class ApprovalStatus(StrEnum):
    APPROVED = "approved"
    PENDING = "pending"
    DENIED = "denied"


class RetentionRule(StrEnum):
    NONE = "none"
    METADATA_ONLY = "metadata_only"
    CANONICAL_FACTS = "canonical_facts"


class CredentialRequirement(StrEnum):
    """Allowed credential/access shapes; this policy never carries credential values."""

    NONE = "none"
    API_KEY_OPTIONAL = "api_key_optional"
    API_KEY_REQUIRED = "api_key_required"
    USER_ID_REQUIRED = "user_id_required"
    IDENTIFYING_USER_AGENT_REQUIRED = "identifying_user_agent_required"
    RIGHTS_REVIEW_REQUIRED = "rights_review_required"
    COMMERCIAL_AGREEMENT_REQUIRED = "commercial_agreement_required"
    COMMERCIAL_AGREEMENT_AND_API_KEY_REQUIRED = "commercial_agreement_and_api_key_required"
    SUBSCRIPTION_REQUIRED = "subscription_required"
    LICENSED_ACCESS_REQUIRED = "licensed_access_required"
    LICENSED_MEDIA_CONTRACT_AND_API_KEY_REQUIRED = "licensed_media_contract_and_api_key_required"
    AUTOMATION_APPROVAL_REQUIRED = "automation_approval_required"
    PROVIDER_CONTRACT_REQUIRED = "provider_contract_required"
    CONTRACT_OR_AUTOMATION_APPROVAL_REQUIRED = "contract_or_automation_approval_required"
    API_KEY_AND_RIGHTS_REVIEW_REQUIRED = "api_key_and_rights_review_required"


class PolicyPurpose(StrEnum):
    INGESTION = "ingestion"
    EDITOR_CONTEXT = "editor_context"
    EXTERNAL_LLM = "external_llm"
    CITATION = "citation"
    RETENTION = "retention"


class SourcePolicyEntry(StrictModel):
    """One reviewable provider/dataset/region production decision, without secrets."""

    policy_id: str = Field(min_length=3, max_length=128)
    provider_id: str = Field(min_length=2, max_length=64)
    dataset: Dataset
    regions: set[Region] = Field(min_length=1)
    owner: str = Field(min_length=2, max_length=128)
    credential_requirement: CredentialRequirement
    ingestion_allowed: bool
    external_llm_allowed: bool
    citation_allowed: bool
    retention_rule: RetentionRule
    approval_status: ApprovalStatus
    production_enabled: bool
    evidence: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_production_enablement(self) -> SourcePolicyEntry:
        if self.production_enabled and (
            self.approval_status is not ApprovalStatus.APPROVED or not self.ingestion_allowed
        ):
            raise ValueError("production_enabled requires approved ingestion policy")
        return self


class SourcePolicyManifest(StrictModel):
    policy_version: str = Field(min_length=1, max_length=64)
    default_decision: str = "deny"
    entries: list[SourcePolicyEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_entries(self) -> SourcePolicyManifest:
        if self.default_decision != "deny":
            raise ValueError("production source policy must deny by default")
        seen: set[tuple[str, Dataset, Region]] = set()
        for entry in self.entries:
            for region in entry.regions:
                key = (entry.provider_id, entry.dataset, region)
                if key in seen:
                    raise ValueError(f"duplicate source policy entry for {key}")
                seen.add(key)
        return self


class PolicyDecision(StrictModel):
    allowed: bool
    provider_id: str
    dataset: Dataset
    region: Region
    purpose: PolicyPurpose
    policy_id: str | None = None
    retention_rule: RetentionRule | None = None
    reason: str


class IngestionRetentionPolicy(StrictModel):
    """Retention limits handed from the production gate to the record-writing handler."""

    rules_by_region: dict[Region, RetentionRule] = Field(min_length=1)

    def rule_for(self, region: Region) -> RetentionRule:
        try:
            return self.rules_by_region[region]
        except KeyError as error:
            raise ValueError(f"missing retention rule for {region.value}") from error


class SourcePolicy(Protocol):
    @property
    def production_enforced(self) -> bool: ...

    def decision(
        self,
        *,
        provider_id: str,
        dataset: Dataset,
        region: Region,
        purpose: PolicyPurpose,
    ) -> PolicyDecision: ...


class SourcePolicyDeniedError(PermissionError):
    def __init__(self, decision: PolicyDecision) -> None:
        super().__init__(
            "source policy denied "
            f"{decision.purpose.value} for {decision.provider_id}/{decision.dataset.value}/"
            f"{decision.region.value}: {decision.reason}"
        )
        self.decision = decision


class ProductionSourcePolicy:
    """Single strict decision seam consumed by ingestion and report-generation paths."""

    def __init__(self, manifest: SourcePolicyManifest) -> None:
        self.manifest = manifest
        self._entries = {
            (entry.provider_id, entry.dataset, region): entry
            for entry in manifest.entries
            for region in entry.regions
        }

    @property
    def production_enforced(self) -> bool:
        return True

    def decision(
        self,
        *,
        provider_id: str,
        dataset: Dataset,
        region: Region,
        purpose: PolicyPurpose,
    ) -> PolicyDecision:
        entry = self._entries.get((provider_id, dataset, region))
        if entry is None:
            return self._decision(
                False,
                provider_id,
                dataset,
                region,
                purpose,
                reason="missing policy entry",
            )
        if entry.approval_status is not ApprovalStatus.APPROVED:
            return self._decision(
                False,
                provider_id,
                dataset,
                region,
                purpose,
                entry=entry,
                reason=f"approval status is {entry.approval_status.value}",
            )

        allowed, reason = self._permission_for(entry, purpose)
        return self._decision(
            allowed,
            provider_id,
            dataset,
            region,
            purpose,
            entry=entry,
            reason=reason,
        )

    def require(
        self,
        *,
        provider_id: str,
        dataset: Dataset,
        region: Region,
        purpose: PolicyPurpose,
    ) -> PolicyDecision:
        decision = self.decision(
            provider_id=provider_id,
            dataset=dataset,
            region=region,
            purpose=purpose,
        )
        if not decision.allowed:
            raise SourcePolicyDeniedError(decision)
        return decision

    @staticmethod
    def _permission_for(entry: SourcePolicyEntry, purpose: PolicyPurpose) -> tuple[bool, str]:
        if purpose in {PolicyPurpose.INGESTION, PolicyPurpose.EDITOR_CONTEXT}:
            if not entry.ingestion_allowed:
                return False, "ingestion is not allowed"
            if not entry.production_enabled:
                return False, "source is not production enabled"
            return True, "approved for production ingestion"
        if purpose is PolicyPurpose.EXTERNAL_LLM:
            return (
                (True, "approved for external LLM")
                if entry.external_llm_allowed
                else (False, "external LLM is not allowed")
            )
        if purpose is PolicyPurpose.CITATION:
            return (
                (True, "approved for citation")
                if entry.citation_allowed
                else (False, "citation is not allowed")
            )
        if purpose is PolicyPurpose.RETENTION:
            return (
                (True, "approved for retention")
                if entry.retention_rule is not RetentionRule.NONE
                else (False, "retention is not allowed")
            )
        raise AssertionError(f"unsupported policy purpose: {purpose}")

    @staticmethod
    def _decision(
        allowed: bool,
        provider_id: str,
        dataset: Dataset,
        region: Region,
        purpose: PolicyPurpose,
        *,
        entry: SourcePolicyEntry | None = None,
        reason: str,
    ) -> PolicyDecision:
        return PolicyDecision(
            allowed=allowed,
            provider_id=provider_id,
            dataset=dataset,
            region=region,
            purpose=purpose,
            policy_id=None if entry is None else entry.policy_id,
            retention_rule=None if entry is None else entry.retention_rule,
            reason=reason,
        )


class NonProductionSourcePolicy:
    """Keeps fixture and local-development flows separate from production governance."""

    @property
    def production_enforced(self) -> bool:
        return False

    def decision(
        self,
        *,
        provider_id: str,
        dataset: Dataset,
        region: Region,
        purpose: PolicyPurpose,
    ) -> PolicyDecision:
        return PolicyDecision(
            allowed=True,
            provider_id=provider_id,
            dataset=dataset,
            region=region,
            purpose=purpose,
            reason="source policy is not enforced outside production",
        )


def load_production_source_policy() -> ProductionSourcePolicy:
    resource = files("macro_platform.governance").joinpath("production_source_policy.json")
    manifest = SourcePolicyManifest.model_validate_json(resource.read_text(encoding="utf-8"))
    return ProductionSourcePolicy(manifest)
