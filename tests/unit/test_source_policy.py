from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from macro_platform.contracts.common import Region
from macro_platform.contracts.provider import Dataset
from macro_platform.governance.source_policy import (
    ApprovalStatus,
    PolicyPurpose,
    ProductionSourcePolicy,
    RetentionRule,
    SourcePolicyEntry,
    SourcePolicyManifest,
    load_production_source_policy,
)


def _entry(
    *,
    credential_requirement: str = "none",
    approval_status: ApprovalStatus = ApprovalStatus.APPROVED,
    production_enabled: bool = True,
    ingestion_allowed: bool = True,
    external_llm_allowed: bool = True,
    citation_allowed: bool = True,
    retention_rule: RetentionRule = RetentionRule.CANONICAL_FACTS,
) -> SourcePolicyEntry:
    return SourcePolicyEntry(
        policy_id="test.us.treasury.market-observations",
        provider_id="test.us.treasury.v1",
        dataset=Dataset.MARKET_OBSERVATIONS,
        regions={Region.US},
        owner="@kazming666",
        credential_requirement=credential_requirement,
        ingestion_allowed=ingestion_allowed,
        external_llm_allowed=external_llm_allowed,
        citation_allowed=citation_allowed,
        retention_rule=retention_rule,
        approval_status=approval_status,
        production_enabled=production_enabled,
        evidence=["docs/data-sources/us-treasury-interest-rates.md"],
    )


def _policy(*entries: SourcePolicyEntry) -> ProductionSourcePolicy:
    return ProductionSourcePolicy(
        SourcePolicyManifest(policy_version="test", entries=list(entries))
    )


def test_gov_026_missing_and_pending_policies_are_denied_by_default() -> None:
    policy = _policy(_entry(approval_status=ApprovalStatus.PENDING, production_enabled=False))

    missing = policy.decision(
        provider_id="unknown.provider.v1",
        dataset=Dataset.MARKET_OBSERVATIONS,
        region=Region.US,
        purpose=PolicyPurpose.INGESTION,
    )
    pending = policy.decision(
        provider_id="test.us.treasury.v1",
        dataset=Dataset.MARKET_OBSERVATIONS,
        region=Region.US,
        purpose=PolicyPurpose.EDITOR_CONTEXT,
    )

    assert not missing.allowed
    assert missing.policy_id is None
    assert missing.reason == "missing policy entry"
    assert not pending.allowed
    assert pending.reason == "approval status is pending"


def test_gov_026_production_enablement_requires_approved_ingestion_policy() -> None:
    with pytest.raises(ValidationError, match="production_enabled requires approved ingestion"):
        _entry(approval_status=ApprovalStatus.PENDING, production_enabled=True)

    with pytest.raises(ValidationError, match="production_enabled requires approved ingestion"):
        _entry(ingestion_allowed=False, production_enabled=True)


def test_gov_026_credential_requirement_rejects_free_text_and_secret_like_values() -> None:
    with pytest.raises(ValidationError, match="credential_requirement"):
        _entry(credential_requirement="Bearer actual-secret-value")


def test_gov_026_policy_exposes_llm_citation_and_retention_decisions() -> None:
    policy = _policy(
        _entry(
            external_llm_allowed=False,
            citation_allowed=True,
            retention_rule=RetentionRule.METADATA_ONLY,
        )
    )

    llm = policy.decision(
        provider_id="test.us.treasury.v1",
        dataset=Dataset.MARKET_OBSERVATIONS,
        region=Region.US,
        purpose=PolicyPurpose.EXTERNAL_LLM,
    )
    citation = policy.decision(
        provider_id="test.us.treasury.v1",
        dataset=Dataset.MARKET_OBSERVATIONS,
        region=Region.US,
        purpose=PolicyPurpose.CITATION,
    )
    retention = policy.decision(
        provider_id="test.us.treasury.v1",
        dataset=Dataset.MARKET_OBSERVATIONS,
        region=Region.US,
        purpose=PolicyPurpose.RETENTION,
    )

    assert not llm.allowed
    assert llm.reason == "external LLM is not allowed"
    assert citation.allowed
    assert retention.allowed
    assert retention.retention_rule is RetentionRule.METADATA_ONLY


def test_gov_026_not_production_enabled_is_denied_for_llm_and_citation() -> None:
    policy = _policy(_entry(production_enabled=False))

    for purpose in (PolicyPurpose.EXTERNAL_LLM, PolicyPurpose.CITATION):
        decision = policy.decision(
            provider_id="test.us.treasury.v1",
            dataset=Dataset.MARKET_OBSERVATIONS,
            region=Region.US,
            purpose=purpose,
        )

        assert not decision.allowed
        assert decision.reason == "source is not production enabled"


def test_gov_026_twelve_data_basic_bars_allows_internal_retention_and_rejects_external_use() -> (
    None
):
    policy = load_production_source_policy()

    ingestion = policy.decision(
        provider_id="us.twelve-data.v1",
        dataset=Dataset.BARS,
        region=Region.US,
        purpose=PolicyPurpose.INGESTION,
    )
    retention = policy.decision(
        provider_id="us.twelve-data.v1",
        dataset=Dataset.BARS,
        region=Region.US,
        purpose=PolicyPurpose.RETENTION,
    )
    external_llm = policy.decision(
        provider_id="us.twelve-data.v1",
        dataset=Dataset.BARS,
        region=Region.US,
        purpose=PolicyPurpose.EXTERNAL_LLM,
    )
    citation = policy.decision(
        provider_id="us.twelve-data.v1",
        dataset=Dataset.BARS,
        region=Region.US,
        purpose=PolicyPurpose.CITATION,
    )
    allowed_symbol = policy.decision(
        provider_id="us.twelve-data.v1",
        dataset=Dataset.BARS,
        region=Region.US,
        purpose=PolicyPurpose.INGESTION,
        source_symbol="SPY",
    )
    denied_symbol = policy.decision(
        provider_id="us.twelve-data.v1",
        dataset=Dataset.BARS,
        region=Region.US,
        purpose=PolicyPurpose.INGESTION,
        source_symbol="IWM",
    )

    assert ingestion.allowed
    assert ingestion.allowed_symbols == frozenset({"DIA", "QQQ", "SPY"})
    assert retention.allowed
    assert retention.retention_rule is RetentionRule.CANONICAL_FACTS
    assert not external_llm.allowed
    assert external_llm.reason == "external LLM is not allowed"
    assert not citation.allowed
    assert citation.reason == "citation is not allowed"
    assert allowed_symbol.allowed
    assert not denied_symbol.allowed
    assert denied_symbol.reason == "source symbol is not allowed"


def test_gov_026_packaged_policy_is_cross_region_and_traceable() -> None:
    policy = load_production_source_policy()
    entries = policy.manifest.entries

    assert {region for entry in entries for region in entry.regions} == {
        Region.CN,
        Region.HK,
        Region.US,
    }
    assert all(entry.evidence for entry in entries)
    assert all(
        Path(evidence.partition("#")[0]).is_file()
        for entry in entries
        for evidence in entry.evidence
    )
    assert all(
        entry.approval_status is ApprovalStatus.APPROVED and entry.ingestion_allowed
        for entry in entries
        if entry.production_enabled
    )
