from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from macro_platform.services.report_validation import (
    ReportFallbackBuilder,
    ReportValidationService,
    ReportValidator,
)
from macro_platform.storage.reporting import ReportInputSnapshot, StoredDailyReport

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 7, 23, 0, 15, tzinfo=UTC)


def _success_payload() -> dict[str, Any]:
    return json.loads((ROOT / "tests/golden/daily_report_v1_success.json").read_text())


def _snapshot() -> ReportInputSnapshot:
    report = _success_payload()
    input_snapshot = report["input_snapshot"]
    source_refs = report["sections"]["source_references"]["items"]
    fact_ids = input_snapshot["fact_ids"]
    source_ids_by_fact = {fact_id: _source_ids_for_fact(report, fact_id) for fact_id in fact_ids}
    facts = [
        {
            "fact_id": fact_id,
            "section_id": _section_for_fact(report, fact_id),
            "label": fact_id,
            "display_text": f"已验证事实 {fact_id}",
            "value": 0.82 if fact_id == "fact.market.cn.index.csi300.change_pct" else None,
            "unit": "percent" if fact_id == "fact.market.cn.index.csi300.change_pct" else None,
            "available_at": input_snapshot["cutoff_at"],
            "report_date": report["report_date"],
            "source_ref_ids": source_ids_by_fact[fact_id],
        }
        for fact_id in fact_ids
    ]
    payload = {
        **input_snapshot,
        "facts": facts,
        "source_references": source_refs,
        "input_quality": {
            input_id: {"status": "available", "required": True}
            for input_id in (
                "market.cn.core_indices.previous_close",
                "news.cn.official_headlines_24h",
                "market.hk.core_indices.previous_close",
                "news.hk.official_headlines_24h",
                "market.us.core_indices.previous_close",
                "calendar.macro_releases_7d",
                "calendar.us_macro_releases_7d",
            )
        },
    }
    return ReportInputSnapshot(
        snapshot_id=input_snapshot["snapshot_id"],
        snapshot_version=input_snapshot["snapshot_version"],
        report_date=date.fromisoformat(report["report_date"]),
        as_of=datetime.fromisoformat(input_snapshot["as_of"].replace("Z", "+00:00")),
        cutoff_at=datetime.fromisoformat(input_snapshot["cutoff_at"].replace("Z", "+00:00")),
        fingerprint_sha256=input_snapshot["fingerprint_sha256"],
        fact_ids=fact_ids,
        payload=payload,
    )


def _walk(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value, *[item for child in value.values() for item in _walk(child)]]
    if isinstance(value, list):
        return [item for child in value for item in _walk(child)]
    return []


def _section_for_fact(report: dict[str, Any], fact_id: str) -> str:
    for section_id, section in report["sections"].items():
        if any(fact_id in item.get("fact_ids", []) for item in _walk(section)):
            return section_id
    raise AssertionError(fact_id)


def _source_ids_for_fact(report: dict[str, Any], fact_id: str) -> list[str]:
    for section in report["sections"].values():
        for item in _walk(section):
            if fact_id in item.get("fact_ids", []):
                return item.get("source_ref_ids", [])
    raise AssertionError(fact_id)


def _stored_report(payload: dict[str, Any]) -> StoredDailyReport:
    return StoredDailyReport(
        report_id=payload["report_id"],
        report_date=date.fromisoformat(payload["report_date"]),
        report_version="v1",
        contract_version=payload["contract_version"],
        input_snapshot_id=payload["input_snapshot"]["snapshot_id"],
        status=payload["status"],
        publication_decision=payload["publication"]["decision"],
        generated_at=datetime.fromisoformat(payload["generated_at"].replace("Z", "+00:00")),
        payload=payload,
    )


class _ValidationStore:
    def __init__(self, snapshot: ReportInputSnapshot) -> None:
        self.snapshot = snapshot
        self.saved: StoredDailyReport | None = None
        self.updated: StoredDailyReport | None = None

    async def load_input_snapshot(self, snapshot_id: str) -> ReportInputSnapshot | None:
        return self.snapshot if snapshot_id == self.snapshot.snapshot_id else None

    async def put_report(self, report: StoredDailyReport) -> bool:
        self.saved = report
        return True

    async def update_report_validation(
        self, report: StoredDailyReport, *, expected_lifecycle_status: str
    ) -> bool:
        assert expected_lifecycle_status == "generated"
        self.updated = report
        return True


def test_rpt_031_fabricated_number_is_rejected() -> None:
    payload = _success_payload()
    payload["sections"]["key_movements"]["claims"] = [
        {
            "claim_type": "number",
            "fact_id": "fact.market.cn.index.csi300.change_pct",
            "value": 0.99,
            "unit": "percent",
        }
    ]

    result = ReportValidator().validate(_stored_report(payload), _snapshot())

    assert not result.publishable
    assert {issue.code for issue in result.issues} == {"FACT_VALUE_MISMATCH"}


def test_rpt_031_source_metadata_mismatch_is_rejected() -> None:
    payload = _success_payload()
    payload["sections"]["source_references"]["items"][0]["provider_record_id"] = "tampered"

    result = ReportValidator().validate(_stored_report(payload), _snapshot())

    assert not result.publishable
    assert {issue.code for issue in result.issues} == {"SOURCE_RECORD_MISMATCH"}


def test_rpt_031_snapshot_identity_mismatch_is_rejected() -> None:
    payload = _success_payload()
    payload["input_snapshot"]["fingerprint_sha256"] = "f" * 64

    result = ReportValidator().validate(_stored_report(payload), _snapshot())

    assert not result.publishable
    assert {issue.code for issue in result.issues} == {"SNAPSHOT_IDENTITY_MISMATCH"}


def test_rpt_031_nested_fact_without_citation_is_rejected() -> None:
    payload = _success_payload()
    payload["sections"]["key_movements"]["items"][0].pop("source_ref_ids")

    result = ReportValidator().validate(_stored_report(payload), _snapshot())

    assert not result.publishable
    assert {issue.code for issue in result.issues} == {"FACT_UNCITED"}


def test_rpt_031_legacy_source_rights_do_not_block_stale_required_input() -> None:
    payload = _success_payload()
    payload["sections"]["key_movements"]["source_ref_ids"] = ["src-cn-market-1"]
    snapshot = _snapshot()
    snapshot = snapshot.model_copy(
        update={
            "payload": {
                **snapshot.payload,
                "source_references": [
                    {
                        **source,
                        "citation_allowed": False,
                    }
                    for source in snapshot.payload["source_references"]
                ],
                "input_quality": {
                    **snapshot.payload["input_quality"],
                    "market.hk.core_indices.previous_close": {
                        "status": "stale",
                        "required": True,
                        "reason": "older than the report freshness limit",
                    },
                },
            }
        }
    )

    result = ReportValidator().validate(_stored_report(payload), snapshot)

    assert not result.publishable
    assert {issue.code for issue in result.issues} == {
        "STALE_REQUIRED_INPUT",
    }


def test_rpt_031_required_quality_input_cannot_be_downgraded() -> None:
    payload = _success_payload()
    base_snapshot = _snapshot()
    snapshot = base_snapshot.model_copy(
        update={
            "payload": {
                **base_snapshot.payload,
                "input_quality": {
                    **base_snapshot.payload["input_quality"],
                    "market.hk.core_indices.previous_close": {
                        "status": "available",
                        "required": False,
                    },
                },
            }
        }
    )

    result = ReportValidator().validate(_stored_report(payload), snapshot)

    assert not result.publishable
    assert {issue.code for issue in result.issues} == {
        "REQUIRED_INPUT_DECLARATION_INVALID",
    }


def test_rpt_029_retryable_required_input_blocks_until_the_worker_recovers() -> None:
    payload = _success_payload()
    base_snapshot = _snapshot()
    snapshot = base_snapshot.model_copy(
        update={
            "payload": {
                **base_snapshot.payload,
                "input_quality": {
                    **base_snapshot.payload["input_quality"],
                    "market.hk.core_indices.previous_close": {
                        "status": "retryable",
                        "required": True,
                        "reason": "provider retry budget is not exhausted",
                    },
                },
            }
        }
    )

    result = ReportValidator().validate(_stored_report(payload), snapshot)

    assert not result.publishable
    assert {issue.code for issue in result.issues} == {"RETRYABLE_REQUIRED_INPUT"}


def test_rpt_029_fallback_exposes_optional_revision_in_data_quality() -> None:
    snapshot = _snapshot()
    revised_snapshot = snapshot.model_copy(
        update={
            "payload": {
                **snapshot.payload,
                "input_quality": {
                    **snapshot.payload["input_quality"],
                    "market.us.vix": {
                        "status": "revised",
                        "required": False,
                        "reason": "provider corrected the close",
                    },
                },
            }
        }
    )

    report = ReportFallbackBuilder().build(
        revised_snapshot,
        report_id="daily-report-fallback-revised",
        report_version="fallback-v1",
        generated_at=NOW,
    )

    assert report.status == "degraded"
    assert report.payload["data_quality"]["revised_inputs"] == [
        {
            "input_id": "market.us.vix",
            "reason_code": "REVISED_OPTIONAL_INPUT",
            "reason": "provider corrected the close",
        }
    ]


def test_rpt_031_fallback_is_deterministic_when_validated_facts_are_sufficient() -> None:
    snapshot = _snapshot()

    first = ReportFallbackBuilder().build(
        snapshot,
        report_id="daily-report-fallback",
        report_version="fallback-v1",
        generated_at=NOW,
    )
    second = ReportFallbackBuilder().build(
        snapshot,
        report_id="daily-report-fallback",
        report_version="fallback-v1",
        generated_at=NOW,
    )

    assert first.payload == second.payload
    assert first.status == "complete"
    assert first.publication_decision == "published"
    assert ReportValidator().validate(first, snapshot).publishable


def test_rpt_031_fallback_preserves_fact_observation_periods() -> None:
    snapshot = _snapshot()
    facts = [
        {
            **fact,
            "claim_type": "number",
            "value": 0.82,
            "unit": "percent",
            "period_start": "2026-07-01",
            "period_end": "2026-07-23",
        }
        if fact["fact_id"] == "fact.market.cn.index.csi300.change_pct"
        else fact
        for fact in snapshot.payload["facts"]
    ]
    snapshot = snapshot.model_copy(update={"payload": {**snapshot.payload, "facts": facts}})

    report = ReportFallbackBuilder().build(
        snapshot,
        report_id="daily-report-fallback-period",
        report_version="fallback-v1",
        generated_at=NOW,
    )
    result = ReportValidator().validate(report, snapshot)

    assert result.publishable
    claim = report.payload["sections"]["key_movements"]["claims"][0]
    assert claim["period_start"] == "2026-07-01"
    assert claim["period_end"] == "2026-07-23"


def test_rpt_031_fallback_fails_closed_when_required_input_is_missing() -> None:
    snapshot = _snapshot()
    quality = dict(snapshot.payload["input_quality"])
    quality.pop("market.hk.core_indices.previous_close")
    blocked_snapshot = snapshot.model_copy(
        update={"payload": {**snapshot.payload, "input_quality": quality}}
    )

    report = ReportFallbackBuilder().build(
        blocked_snapshot,
        report_id="daily-report-fallback-blocked",
        report_version="fallback-v1",
        generated_at=NOW,
    )

    assert report.status == "incomplete"
    assert report.publication_decision == "not_published"
    assert not ReportValidator().validate(report, blocked_snapshot).publishable


def test_rpt_029_fallback_fails_closed_when_snapshot_has_no_materialized_facts() -> None:
    snapshot = _snapshot()
    blocked_snapshot = snapshot.model_copy(
        update={
            "fact_ids": [],
            "payload": {**snapshot.payload, "fact_ids": [], "facts": []},
        }
    )

    report = ReportFallbackBuilder().build(
        blocked_snapshot,
        report_id="daily-report-fallback-no-facts",
        report_version="fallback-v1",
        generated_at=NOW,
    )

    assert report.status == "incomplete"
    assert report.publication_decision == "not_published"
    assert report.payload["data_quality"]["unavailable_inputs"] == [
        {
            "input_id": "report.facts",
            "reason_code": "REQUIRED_FACTS_UNAVAILABLE",
            "reason": "approved input snapshot contains no materialized report facts",
        }
    ]


async def test_rpt_031_validation_service_persists_validated_state() -> None:
    snapshot = _snapshot()
    candidate = _stored_report(_success_payload())
    store = _ValidationStore(snapshot)

    result = await ReportValidationService().validate_or_fallback(
        store,
        snapshot_id=snapshot.snapshot_id,
        report_id=candidate.report_id,
        report_version=candidate.report_version,
        generated_at=NOW,
        candidate=candidate,
    )

    assert result.used_fallback is False
    assert result.issues == ()
    assert result.report.lifecycle_status == "validated"
    assert result.report.publication_decision == "published"
    assert store.updated == result.report


async def test_rpt_031_validation_service_persists_failed_validation_errors() -> None:
    payload = _success_payload()
    payload["sections"]["key_movements"]["claims"] = [
        {
            "claim_type": "number",
            "fact_id": "fact.market.cn.index.csi300.change_pct",
            "value": 0.99,
            "unit": "percent",
        }
    ]
    snapshot = _snapshot()
    candidate = _stored_report(payload)
    store = _ValidationStore(snapshot)

    result = await ReportValidationService().validate_or_fallback(
        store,
        snapshot_id=snapshot.snapshot_id,
        report_id=candidate.report_id,
        report_version=candidate.report_version,
        generated_at=NOW,
        candidate=candidate,
    )

    assert result.report.lifecycle_status == "failed"
    assert result.report.status == "incomplete"
    assert result.report.publication_decision == "not_published"
    assert [issue.code for issue in result.report.validation_errors] == ["FACT_VALUE_MISMATCH"]
    assert store.updated == result.report


async def test_rpt_031_validation_service_persists_deterministic_fallback() -> None:
    snapshot = _snapshot()
    store = _ValidationStore(snapshot)

    result = await ReportValidationService().validate_or_fallback(
        store,
        snapshot_id=snapshot.snapshot_id,
        report_id="daily-report-fallback-service",
        report_version="fallback-v1",
        generated_at=NOW,
        candidate=None,
    )

    assert result.used_fallback is True
    assert result.issues == ()
    assert result.report.lifecycle_status == "validated"
    assert store.saved == result.report
