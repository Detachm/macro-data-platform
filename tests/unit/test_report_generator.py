from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from macro_platform.contracts.common import Region
from macro_platform.contracts.report import DailyReport
from macro_platform.services.llm import (
    LlmError,
    LlmResponse,
    LlmStructuredOutputError,
    LlmTimeoutError,
)
from macro_platform.services.report_day_policy import ExchangeReportDayPolicy
from macro_platform.services.report_generator import (
    DailyReportInputPreset,
    ReportGenerationService,
    ReportPromptBuilder,
)
from macro_platform.storage.reporting import (
    ReportGenerationAttempt,
    ReportInputSnapshot,
    StoredDailyReport,
)

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 7, 23, 0, 20, 4, tzinfo=UTC)


def _success_payload() -> dict[str, Any]:
    return json.loads((ROOT / "tests/golden/daily_report_v1_success.json").read_text())


def _snapshot() -> ReportInputSnapshot:
    report = _success_payload()
    input_snapshot = report["input_snapshot"]
    source_ref_ids = [
        item["source_ref_id"] for item in report["sections"]["source_references"]["items"]
    ]
    facts = [
        {
            "fact_id": fact_id,
            "section_id": "key_movements",
            "display_text": f"已验证事实 {fact_id}",
            "available_at": input_snapshot["cutoff_at"],
            "report_date": report["report_date"],
            "source_ref_ids": source_ref_ids,
        }
        for fact_id in input_snapshot["fact_ids"]
    ]
    payload = {
        **input_snapshot,
        "editor_context": {"facts": ["approved fact payload"]},
        "source_ref_ids": source_ref_ids,
        "facts": facts,
        "source_references": report["sections"]["source_references"]["items"],
        "input_quality": {
            input_id: {"status": "available", "required": True}
            for input_id in (
                "market.cn.core_indices.previous_close",
                "news.cn.official_headlines_24h",
                "market.hk.core_indices.previous_close",
                "news.hk.official_headlines_24h",
                "market.us.core_indices.previous_close",
                "calendar.macro_releases_7d",
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
        fact_ids=input_snapshot["fact_ids"],
        payload=payload,
    )


class _FakeLlm:
    def __init__(self, responses: Sequence[object]) -> None:
        self._responses = list(responses)
        self.requests: list[object] = []

    async def generate(self, request: object) -> LlmResponse:
        self.requests.append(request)
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response  # type: ignore[return-value]


class _FakeReportStore:
    def __init__(self, snapshot: ReportInputSnapshot) -> None:
        self.snapshot = snapshot
        self.attempts: list[ReportGenerationAttempt] = []
        self.reports: list[StoredDailyReport] = []
        self.accept_attempt = True
        self.accept_report = True

    async def load_input_snapshot(self, snapshot_id: str) -> ReportInputSnapshot | None:
        return self.snapshot if snapshot_id == self.snapshot.snapshot_id else None

    async def put_generation_attempt(self, attempt: ReportGenerationAttempt) -> bool:
        self.attempts.append(attempt)
        return self.accept_attempt

    async def update_generation_attempt(self, attempt: ReportGenerationAttempt) -> None:
        self.attempts[-1] = attempt

    async def put_report(self, report: StoredDailyReport) -> bool:
        if self.accept_report:
            self.reports.append(report)
        return self.accept_report

    async def update_report_validation(
        self, report: StoredDailyReport, *, expected_lifecycle_status: str
    ) -> bool:
        assert expected_lifecycle_status == "generated"
        assert self.reports
        self.reports[-1] = report
        return True


def test_rpt_030_daily_report_contract_accepts_canonical_fixture() -> None:
    report = DailyReport.model_validate(_success_payload())

    assert report.contract_name == "DailyReport"
    assert set(report.sections) == {
        "executive_summary",
        "cn_highlights",
        "hk_highlights",
        "us_highlights",
        "key_movements",
        "upcoming_calendar",
        "data_quality_notice",
        "source_references",
    }


def test_rpt_030_input_preset_is_point_in_time_and_versioned() -> None:
    request = DailyReportInputPreset().request(as_of=NOW)

    assert request.preset_id == "daily_macro_v1"
    assert DailyReportInputPreset.version == "1.0.0"
    assert request.require_point_in_time is True
    assert request.regions == {Region.CN, Region.HK, Region.US}
    assert request.market.instrument_ids == ["ins_cn_csi300", "ins_hk_hsi", "ins_us_spx"]


def test_rpt_051_report_generation_overrides_model_calendar_with_persisted_policy() -> None:
    report_date = date(2026, 7, 26)
    snapshot = _snapshot().model_copy(deep=True, update={"report_date": report_date})
    snapshot.payload["report_day_policy"] = (
        ExchangeReportDayPolicy().evaluate(report_date).to_payload()
    )
    model_output = _success_payload()
    model_output["calendar"] = {"day_type": "business_day", "holiday_notice": None}

    payload = ReportGenerationService(_FakeLlm([]), clock=lambda: NOW)._materialize_report(
        LlmResponse(structured_output=model_output),
        snapshot=snapshot,
        report_id="daily-report-2026-07-26-v1",
        generated_at=NOW,
    )

    assert payload["calendar"] == {
        "day_type": "weekend",
        "holiday_notice": "周末，CN/HK/US 市场休市；本期发布宏观消息、日历和分析。",
    }


def test_rpt_030_prompt_builder_ignores_legacy_external_llm_rights() -> None:
    snapshot = _snapshot()
    denied = snapshot.model_copy(deep=True)
    denied.payload["source_references"][0]["external_llm_allowed"] = False

    request = ReportPromptBuilder().build(denied, model="test-model", parameters={})
    assert request.source_ref_ids == denied.payload["source_ref_ids"]


def test_rpt_030_prompt_builder_allows_news_body_in_internal_use() -> None:
    snapshot = _snapshot()
    internal = snapshot.model_copy(deep=True)
    internal.payload["editor_context"]["news_events"] = [{"body": "full internal article"}]

    request = ReportPromptBuilder().build(internal, model="test-model", parameters={})
    assert (
        request.input_payload["editor_context"]["news_events"][0]["body"] == "full internal article"
    )


def test_rpt_030_prompt_builder_rejects_restricted_snapshot_payload() -> None:
    snapshot = _snapshot()
    unsafe = snapshot.model_copy(
        update={
            "payload": {
                **snapshot.payload,
                "editor_context": {"api_token": "must-not-enter-a-prompt"},
            }
        }
    )

    with pytest.raises(ValueError, match="restricted"):
        ReportPromptBuilder().build(unsafe, model="test-model", parameters={})


@pytest.mark.asyncio
async def test_rpt_030_generation_retries_timeout_and_records_trace() -> None:
    snapshot = _snapshot()
    llm = _FakeLlm(
        [
            LlmTimeoutError("temporary timeout"),
            LlmResponse(structured_output=_success_payload()),
        ]
    )
    store = _FakeReportStore(snapshot)

    result = await ReportGenerationService(
        llm,
        clock=lambda: NOW,
        max_attempts=2,
    ).generate(
        store,
        snapshot_id=snapshot.snapshot_id,
        report_id="daily-report-2026-07-23-v2",
        report_version="v2",
        model="test-model",
        parameters={"temperature": 0},
    )

    assert result.report is not None
    assert result.report.lifecycle_status == "validated"
    assert result.report.payload["publication"]["decision"] == "published"
    assert result.attempt.lifecycle_status == "generated"
    assert result.attempt.attempt_no == 2
    assert result.attempt.prompt_version == "daily-report-v1.0"
    assert result.attempt.model == "test-model"
    assert result.attempt.input_fingerprint_sha256 == snapshot.fingerprint_sha256
    assert result.attempt.source_ref_ids == snapshot.payload["source_ref_ids"]
    assert len(llm.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("input_status", "error_code"),
    [
        ("missing", "REPORT_INPUT_QUALITY_BLOCKED"),
        ("quarantined", "REPORT_INPUT_QUALITY_BLOCKED"),
        ("retryable", "REPORT_INPUT_QUALITY_RETRYABLE"),
    ],
)
async def test_rpt_029_quality_gate_rejects_required_input_before_calling_llm(
    input_status: str, error_code: str
) -> None:
    snapshot = _snapshot().model_copy(deep=True)
    snapshot.payload["input_quality"]["market.us.core_indices.previous_close"] = {
        "status": input_status,
        "required": True,
        "reason": "scheduled quality evidence requires a stop",
    }
    llm = _FakeLlm([AssertionError("quality-blocked snapshot must not reach the LLM")])
    store = _FakeReportStore(snapshot)

    result = await ReportGenerationService(llm, clock=lambda: NOW).generate(
        store,
        snapshot_id=snapshot.snapshot_id,
        report_id=f"daily-report-quality-{input_status}",
        report_version="v1",
        model="test-model",
        parameters={},
    )

    assert result.report is None
    assert result.attempt.lifecycle_status == "failed"
    assert result.attempt.error_code == error_code
    assert result.attempt.source_ref_ids == snapshot.payload["source_ref_ids"]
    assert llm.requests == []


@pytest.mark.asyncio
async def test_rpt_030_malformed_output_is_persisted_as_failed() -> None:
    snapshot = _snapshot()
    llm = _FakeLlm(
        [
            LlmResponse(structured_output={"not": "a report"}),
            LlmStructuredOutputError("still malformed"),
        ]
    )
    store = _FakeReportStore(snapshot)

    result = await ReportGenerationService(
        llm,
        clock=lambda: NOW,
        max_attempts=2,
    ).generate(
        store,
        snapshot_id=snapshot.snapshot_id,
        report_id="daily-report-2026-07-23-failed",
        report_version="failed-v1",
        model="test-model",
        parameters={},
    )

    assert result.report is not None
    assert result.report.status == "complete"
    assert result.report.publication_decision == "published"
    assert result.attempt.lifecycle_status == "failed"
    assert result.attempt.error_code == "MALFORMED_STRUCTURED_OUTPUT"
    assert len(store.reports) == 1


@pytest.mark.asyncio
async def test_rpt_031_llm_unavailable_uses_failsafe_fallback() -> None:
    snapshot = _snapshot()
    store = _FakeReportStore(snapshot)

    result = await ReportGenerationService(
        _FakeLlm([LlmError("provider unavailable")]),
        clock=lambda: NOW,
        max_attempts=1,
    ).generate(
        store,
        snapshot_id=snapshot.snapshot_id,
        report_id="daily-report-2026-07-23-unavailable",
        report_version="fallback-v1",
        model="test-model",
        parameters={},
    )

    assert result.report is not None
    assert result.report.status == "complete"
    assert result.attempt.lifecycle_status == "failed"
    assert result.attempt.error_code == "LLM_UNAVAILABLE"


@pytest.mark.asyncio
async def test_rpt_030_existing_report_version_is_not_overwritten() -> None:
    snapshot = _snapshot()
    llm = _FakeLlm([LlmResponse(structured_output=_success_payload())])
    store = _FakeReportStore(snapshot)
    store.accept_attempt = True
    store.accept_report = False

    result = await ReportGenerationService(llm, clock=lambda: NOW).generate(
        store,
        snapshot_id=snapshot.snapshot_id,
        report_id="daily-report-2026-07-23-existing",
        report_version="v1",
        model="test-model",
        parameters={},
    )

    assert result.report is None
    assert result.attempt.lifecycle_status == "failed"
    assert result.attempt.error_code == "REPORT_ALREADY_EXISTS"


@pytest.mark.asyncio
async def test_rpt_031_rejects_unknown_nested_fact_ids_and_uses_fallback() -> None:
    snapshot = _snapshot()
    output = _success_payload()
    output["sections"]["key_movements"]["items"][0]["fact_ids"] = ["unknown-fact"]
    store = _FakeReportStore(snapshot)

    result = await ReportGenerationService(
        _FakeLlm([LlmResponse(structured_output=output)]),
        clock=lambda: NOW,
        max_attempts=1,
    ).generate(
        store,
        snapshot_id=snapshot.snapshot_id,
        report_id="daily-report-2026-07-23-invalid-fact",
        report_version="invalid-fact-v1",
        model="test-model",
        parameters={},
    )

    assert result.report is not None
    assert result.report.lifecycle_status == "validated"
    assert result.report.publication_decision == "published"
    assert result.attempt.lifecycle_status == "failed"
    assert result.attempt.error_code == "MALFORMED_STRUCTURED_OUTPUT"
    assert len(store.reports) == 1
