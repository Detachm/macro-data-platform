from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import UTC, date, datetime, time
from typing import Any, Protocol
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from macro_platform.contracts.common import Region
from macro_platform.contracts.editor import EditorContextRequest
from macro_platform.contracts.report import DailyReport
from macro_platform.services.llm import (
    LlmClient,
    LlmRequest,
    LlmResponse,
    LlmStructuredOutputError,
    LlmTimeoutError,
)
from macro_platform.storage.reporting import (
    ReportGenerationAttempt,
    ReportInputSnapshot,
    StoredDailyReport,
)

REPORT_PROMPT_VERSION = "daily-report-v1.0"
REPORT_INPUT_PRESET_ID = "daily_macro_v1"
REPORT_INPUT_PRESET_VERSION = "1.0.0"
_REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")
_REPORT_PUBLISH_TIME = time(8, 30)
_FORBIDDEN_PROMPT_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "body",
        "cookie",
        "credential",
        "password",
        "secret",
        "token",
    }
)


class ReportGenerationStore(Protocol):
    async def load_input_snapshot(self, snapshot_id: str) -> ReportInputSnapshot | None: ...

    async def put_generation_attempt(self, attempt: ReportGenerationAttempt) -> bool: ...

    async def update_generation_attempt(self, attempt: ReportGenerationAttempt) -> None: ...

    async def put_report(self, report: StoredDailyReport) -> bool: ...


class DailyReportInputPreset:
    """Stable EditorContext request shape consumed by the report pipeline."""

    preset_id = REPORT_INPUT_PRESET_ID
    version = REPORT_INPUT_PRESET_VERSION

    def request(
        self,
        *,
        regions: set[Region],
        as_of: datetime,
    ) -> EditorContextRequest:
        return EditorContextRequest(
            as_of=as_of,
            regions=regions,
            preset_id=self.preset_id,
            require_point_in_time=True,
            fail_on_incomplete=False,
        )


class ReportGenerationError(RuntimeError):
    pass


class ReportPromptBuilder:
    """Builds the fixed report prompt from a persisted, already-approved snapshot."""

    prompt_version = REPORT_PROMPT_VERSION

    def build(
        self,
        snapshot: ReportInputSnapshot,
        *,
        model: str,
        parameters: Mapping[str, Any],
    ) -> LlmRequest:
        editor_context = snapshot.payload.get("editor_context")
        if not isinstance(editor_context, dict):
            raise ReportGenerationError(
                "input snapshot does not contain the fixed editor_context payload"
            )
        prompt_payload = {
            "report_date": snapshot.report_date.isoformat(),
            "as_of": snapshot.as_of.isoformat(),
            "cutoff_at": snapshot.cutoff_at.isoformat(),
            "editor_context": deepcopy(editor_context),
        }
        _assert_prompt_safe(prompt_payload)
        _assert_prompt_safe(dict(parameters), path="parameters")

        source_ref_ids = snapshot.payload.get("source_ref_ids", [])
        if not isinstance(source_ref_ids, list) or not all(
            isinstance(source_ref_id, str) for source_ref_id in source_ref_ids
        ):
            raise ReportGenerationError("input snapshot source_ref_ids must be strings")
        return LlmRequest(
            model=model,
            prompt_version=self.prompt_version,
            system_prompt=(
                "Generate only a structured DailyReport v1.0 draft from the supplied facts. "
                "Do not add facts, forecasts, recommendations, or source records. "
                "Keep every fact_id and source_ref_id machine-readable."
            ),
            input_payload=prompt_payload,
            input_fingerprint_sha256=snapshot.fingerprint_sha256,
            source_ref_ids=source_ref_ids,
            parameters=dict(parameters),
        )


class ReportGenerationResult:
    def __init__(
        self,
        *,
        report: StoredDailyReport | None,
        attempt: ReportGenerationAttempt,
    ) -> None:
        self.report = report
        self.attempt = attempt


class ReportGenerationService:
    """Generate a report from persisted facts without a provider dependency."""

    def __init__(
        self,
        llm: LlmClient,
        *,
        clock: Callable[[], datetime],
        prompt_builder: ReportPromptBuilder | None = None,
        timeout_seconds: float = 30,
        max_attempts: int = 2,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("report generation timeout must be positive")
        if max_attempts < 1:
            raise ValueError("report generation max_attempts must be positive")
        self._llm = llm
        self._clock = clock
        self._prompt_builder = prompt_builder or ReportPromptBuilder()
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts

    async def generate(
        self,
        store: ReportGenerationStore,
        *,
        snapshot_id: str,
        report_id: str,
        report_version: str,
        model: str,
        parameters: Mapping[str, Any],
    ) -> ReportGenerationResult:
        snapshot = await store.load_input_snapshot(snapshot_id)
        if snapshot is None:
            raise ReportGenerationError(f"input snapshot does not exist: {snapshot_id}")
        request = self._prompt_builder.build(
            snapshot,
            model=model,
            parameters=parameters,
        )
        attempt = ReportGenerationAttempt(
            generation_id=_generation_id(),
            report_id=report_id,
            report_version=report_version,
            input_snapshot_id=snapshot.snapshot_id,
            prompt_version=request.prompt_version,
            model=model,
            model_parameters=dict(parameters),
            input_fingerprint_sha256=snapshot.fingerprint_sha256,
            source_ref_ids=request.source_ref_ids,
        )
        if not await store.put_generation_attempt(attempt):
            raise ReportGenerationError(
                "report generation already exists; use a new report ID, version, and input snapshot"
            )

        for attempt_no in range(1, self._max_attempts + 1):
            if attempt_no > 1:
                attempt = attempt.model_copy(update={"attempt_no": attempt_no})
                await store.update_generation_attempt(attempt)
            try:
                response = await asyncio.wait_for(
                    self._llm.generate(request), timeout=self._timeout_seconds
                )
                payload = self._materialize_report(
                    response,
                    snapshot=snapshot,
                    report_id=report_id,
                    generated_at=self._clock().astimezone(UTC),
                )
                DailyReport.model_validate(payload)
            except (LlmTimeoutError, TimeoutError):
                error_code = "LLM_TIMEOUT"
            except (LlmStructuredOutputError, ValidationError, ValueError, TypeError):
                error_code = "MALFORMED_STRUCTURED_OUTPUT"
            else:
                report = StoredDailyReport(
                    report_id=report_id,
                    report_date=snapshot.report_date,
                    report_version=report_version,
                    contract_version="1.0",
                    input_snapshot_id=snapshot.snapshot_id,
                    status=payload["status"],
                    publication_decision=payload["publication"]["decision"],
                    generated_at=payload["generated_at"],
                    payload=payload,
                    lifecycle_status="generated",
                    generation_id=attempt.generation_id,
                )
                if not await store.put_report(report):
                    attempt = attempt.model_copy(
                        update={
                            "lifecycle_status": "failed",
                            "error_code": "REPORT_ALREADY_EXISTS",
                        }
                    )
                    await store.update_generation_attempt(attempt)
                    return ReportGenerationResult(report=None, attempt=attempt)
                attempt = attempt.model_copy(
                    update={
                        "lifecycle_status": "generated",
                        "response_payload": response.structured_output,
                        "error_code": None,
                    }
                )
                await store.update_generation_attempt(attempt)
                return ReportGenerationResult(report=report, attempt=attempt)

            if attempt_no == self._max_attempts:
                attempt = attempt.model_copy(
                    update={"lifecycle_status": "failed", "error_code": error_code}
                )
                await store.update_generation_attempt(attempt)
                return ReportGenerationResult(report=None, attempt=attempt)

        raise AssertionError("report generation loop must return")

    def _materialize_report(
        self,
        response: LlmResponse,
        *,
        snapshot: ReportInputSnapshot,
        report_id: str,
        generated_at: datetime,
    ) -> dict[str, Any]:
        output = deepcopy(response.structured_output)
        if "contract_name" not in output:
            output = {
                "contract_name": "DailyReport",
                "contract_version": "1.0",
                "timezone": "Asia/Shanghai",
                "schedule": _default_schedule(),
                "calendar": {"day_type": _day_type(snapshot.report_date), "holiday_notice": None},
                "status": output.get("status", "complete"),
                "data_quality": output.get("data_quality", _complete_quality()),
                "sections": output.get("sections", {}),
                **output,
            }
        output.update(
            {
                "contract_name": "DailyReport",
                "contract_version": "1.0",
                "report_id": report_id,
                "report_date": snapshot.report_date.isoformat(),
                "timezone": "Asia/Shanghai",
                "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
                "input_snapshot": {
                    "snapshot_id": snapshot.snapshot_id,
                    "snapshot_version": snapshot.snapshot_version,
                    "as_of": snapshot.as_of.isoformat().replace("+00:00", "Z"),
                    "cutoff_at": snapshot.cutoff_at.isoformat().replace("+00:00", "Z"),
                    "fingerprint_sha256": snapshot.fingerprint_sha256,
                    "fact_ids": snapshot.fact_ids,
                },
                "publication": {
                    "decision": "not_published",
                    "reason_code": "PENDING_VALIDATION",
                    "scheduled_publish_at": _scheduled_publish_at(snapshot)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "published_at": None,
                },
            }
        )
        return output


def _generation_id() -> UUID:
    return uuid4()


def _assert_prompt_safe(value: Any, *, path: str = "input_payload") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = str(key).lower().replace("-", "_")
            if child is not None and (
                normalized_key in _FORBIDDEN_PROMPT_KEYS
                or any(forbidden in normalized_key for forbidden in ("token", "secret", "password"))
            ):
                raise ValueError(f"restricted field cannot enter report prompt: {path}.{key}")
            _assert_prompt_safe(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_prompt_safe(child, path=f"{path}[{index}]")


def _scheduled_publish_at(snapshot: ReportInputSnapshot) -> datetime:
    local = datetime.combine(snapshot.report_date, _REPORT_PUBLISH_TIME, tzinfo=_REPORT_TIMEZONE)
    return local.astimezone(UTC)


def _day_type(report_date: date) -> str:
    return "weekend" if report_date.weekday() >= 5 else "business_day"


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


def _complete_quality() -> dict[str, Any]:
    return {
        "status": "complete",
        "missing_required_inputs": [],
        "stale_inputs": [],
        "late_inputs": [],
        "revised_inputs": [],
        "unavailable_inputs": [],
    }
