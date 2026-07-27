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
    LlmResponse,
    LlmStructuredOutputError,
    LlmTimeoutError,
)
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
    payload = {
        **input_snapshot,
        "editor_context": {"facts": ["approved fact payload"]},
        "source_ref_ids": source_ref_ids,
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
    request = DailyReportInputPreset().request(regions={Region.CN, Region.HK}, as_of=NOW)

    assert request.preset_id == "daily_macro_v1"
    assert DailyReportInputPreset.version == "1.0.0"
    assert request.require_point_in_time is True
    assert request.regions == {Region.CN, Region.HK}


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
    assert result.report.lifecycle_status == "generated"
    assert result.report.payload["publication"]["decision"] == "not_published"
    assert result.attempt.lifecycle_status == "generated"
    assert result.attempt.attempt_no == 2
    assert result.attempt.prompt_version == "daily-report-v1.0"
    assert result.attempt.model == "test-model"
    assert result.attempt.input_fingerprint_sha256 == snapshot.fingerprint_sha256
    assert result.attempt.source_ref_ids == snapshot.payload["source_ref_ids"]
    assert len(llm.requests) == 2


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

    assert result.report is None
    assert result.attempt.lifecycle_status == "failed"
    assert result.attempt.error_code == "MALFORMED_STRUCTURED_OUTPUT"
    assert not store.reports


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
