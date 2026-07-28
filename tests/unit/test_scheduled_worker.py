from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from structlog.testing import capture_logs

from macro_platform.config import Settings
from macro_platform.jobs.scheduler import (
    RetryableScheduledTaskError,
    ScheduledIngestionWorker,
    ScheduledTaskResult,
    SchedulerNotConfiguredError,
    _first_safe_run_time,
    _run_configured_schedule,
    run_scheduler,
)
from macro_platform.services.report_input_quality import (
    REQUIRED_REPORT_INPUT_IDS,
    ReportInputQualityGate,
)
from macro_platform.storage.reporting import ReportInputSnapshot


def _snapshot(
    input_quality: dict[str, object], *, include_required_inputs: bool = True
) -> ReportInputSnapshot:
    as_of = datetime(2026, 7, 27, 0, 15, tzinfo=UTC)
    payload = {
        "snapshot_id": "snapshot-scheduler-quality-v1",
        "snapshot_version": "1.0",
        "as_of": as_of.isoformat().replace("+00:00", "Z"),
        "cutoff_at": as_of.isoformat().replace("+00:00", "Z"),
        "fingerprint_sha256": "a" * 64,
        "fact_ids": ["fact-scheduler-quality"],
        "facts": [{"fact_id": "fact-scheduler-quality"}],
        "input_quality": {
            **(
                {
                    input_id: {"status": "available", "required": True}
                    for input_id in REQUIRED_REPORT_INPUT_IDS
                }
                if include_required_inputs
                else {}
            ),
            **input_quality,
        },
    }
    return ReportInputSnapshot(
        snapshot_id="snapshot-scheduler-quality-v1",
        snapshot_version="1.0",
        report_date=date(2026, 7, 27),
        as_of=as_of,
        cutoff_at=as_of,
        fingerprint_sha256="a" * 64,
        fact_ids=["fact-scheduler-quality"],
        payload=payload,
    )


def test_rpt_029_required_quarantine_blocks_the_report_quality_gate() -> None:
    result = ReportInputQualityGate().evaluate(
        _snapshot(
            {
                "market.us.core_indices.previous_close": {
                    "status": "quarantined",
                    "required": True,
                    "reason": "one source row failed OHLC validation",
                }
            }
        )
    )

    assert result.status == "blocked"
    assert [(issue.input_id, issue.code) for issue in result.issues] == [
        ("market.us.core_indices.previous_close", "QUARANTINED_REQUIRED_INPUT")
    ]


def test_rpt_029_optional_revision_degrades_but_does_not_block_quality_gate() -> None:
    result = ReportInputQualityGate().evaluate(
        _snapshot(
            {
                "market.us.vix": {
                    "status": "revised",
                    "required": False,
                    "reason": "provider corrected the close",
                }
            }
        )
    )

    assert result.status == "degraded"
    assert [(issue.input_id, issue.code) for issue in result.issues] == [
        ("market.us.vix", "REVISED_OPTIONAL_INPUT")
    ]


def test_rpt_029_legacy_rights_marker_does_not_create_a_rights_gate() -> None:
    result = ReportInputQualityGate().evaluate(
        _snapshot(
            {
                "market.hk.core_indices.previous_close": {
                    "status": "retryable",
                    "required": True,
                    "reason": "provider is rate limited",
                },
                "news.cn.official_headlines_24h": {
                    "status": "denied",
                    "required": True,
                    "reason": "legacy rights marker must not become a runtime gate",
                },
            }
        )
    )

    assert result.status == "retryable"
    assert [(issue.input_id, issue.code) for issue in result.issues] == [
        ("market.hk.core_indices.previous_close", "RETRYABLE_REQUIRED_INPUT"),
    ]


def test_rpt_029_missing_required_quality_result_blocks_the_report() -> None:
    result = ReportInputQualityGate().evaluate(_snapshot({}, include_required_inputs=False))

    assert result.status == "blocked"
    assert {issue.code for issue in result.issues} == {"REQUIRED_INPUT_UNAVAILABLE"}


def test_rpt_029_incomplete_global_macro_calendar_remains_a_required_blocker() -> None:
    result = ReportInputQualityGate().evaluate(
        _snapshot(
            {
                "calendar.macro_releases_7d": {
                    "status": "missing",
                    "required": True,
                    "reason": "HK and US macro release calendars have no approved live provider",
                }
            }
        )
    )

    assert result.status == "blocked"
    assert [(issue.input_id, issue.code) for issue in result.issues] == [
        ("calendar.macro_releases_7d", "MISSING_REQUIRED_INPUT")
    ]


def test_rpt_029_empty_fact_set_blocks_even_when_inputs_are_available() -> None:
    snapshot = _snapshot({}).model_copy(
        update={
            "fact_ids": [],
            "payload": {
                **_snapshot({}).payload,
                "fact_ids": [],
                "facts": [],
            },
        }
    )

    result = ReportInputQualityGate().evaluate(snapshot)

    assert result.status == "blocked"
    assert [(issue.input_id, issue.code) for issue in result.issues] == [
        ("report.facts", "REQUIRED_FACTS_UNAVAILABLE")
    ]


def test_rpt_029_mismatched_fact_set_blocks_even_when_inputs_are_available() -> None:
    snapshot = _snapshot({}).model_copy(
        update={
            "payload": {
                **_snapshot({}).payload,
                "facts": [{"fact_id": "unexpected-fact"}],
            },
        }
    )

    result = ReportInputQualityGate().evaluate(snapshot)

    assert result.status == "blocked"
    assert [(issue.input_id, issue.code) for issue in result.issues] == [
        ("report.facts", "REQUIRED_FACTS_UNAVAILABLE")
    ]


def test_rpt_029_duplicate_declared_fact_id_blocks_even_when_payload_is_nonempty() -> None:
    snapshot = _snapshot({}).model_copy(
        update={
            "fact_ids": ["fact-scheduler-quality", "fact-scheduler-quality"],
            "payload": {
                **_snapshot({}).payload,
                "fact_ids": ["fact-scheduler-quality", "fact-scheduler-quality"],
            },
        }
    )

    result = ReportInputQualityGate().evaluate(snapshot)

    assert result.status == "blocked"
    assert [(issue.input_id, issue.code) for issue in result.issues] == [
        ("report.facts", "REQUIRED_FACTS_UNAVAILABLE")
    ]


def test_job_029_scheduler_starts_collection_before_the_report_cutoff() -> None:
    assert (
        _first_safe_run_time(
            schedule_hour=7,
            schedule_minute=50,
            cutoff_hour=8,
            cutoff_minute=15,
        )
        == datetime(2026, 7, 27, 7, 50).time()
    )


def test_job_029_scheduler_rejects_a_collection_start_at_or_after_cutoff() -> None:
    with pytest.raises(ValueError, match="must be before the report cutoff"):
        _first_safe_run_time(
            schedule_hour=8,
            schedule_minute=15,
            cutoff_hour=8,
            cutoff_minute=15,
        )

    with pytest.raises(ValueError, match="must be before WORKER_REPORT_CUTOFF"):
        Settings(
            worker_schedule_hour_local=8,
            worker_schedule_minute_local=16,
            worker_report_cutoff_hour_local=8,
            worker_report_cutoff_minute_local=15,
        )


@dataclass
class _Task:
    task_id: str
    required: bool = True
    outcomes: list[Exception | ScheduledTaskResult | None] = field(default_factory=lambda: [None])
    report_dates: list[date] = field(default_factory=list)

    async def run(self, report_date: date) -> ScheduledTaskResult:
        self.report_dates.append(report_date)
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            if isinstance(outcome, ScheduledTaskResult):
                return outcome
            raise outcome
        return ScheduledTaskResult(
            task_id=self.task_id,
            provider_role="test.provider.primary",
            status="succeeded",
            run_id=uuid4(),
        )


class _Lock:
    def __init__(self, *, acquired: bool = True) -> None:
        self.acquired = acquired
        self.report_dates: list[date] = []

    @asynccontextmanager
    async def hold(self, report_date: date) -> AsyncIterator[bool]:
        self.report_dates.append(report_date)
        yield self.acquired


@pytest.mark.asyncio
async def test_job_029_worker_retries_then_records_a_required_task_result() -> None:
    task = _Task(
        task_id="us.daily-bars",
        outcomes=[RetryableScheduledTaskError("temporary upstream failure"), None],
    )
    delays: list[float] = []
    worker = ScheduledIngestionWorker(
        tasks=[task],
        report_date_lock=_Lock(),
        max_attempts=2,
        sleeper=lambda delay: _record_delay(delays, delay),
    )

    result = await worker.run_for_date(date(2026, 7, 27))

    assert result.status == "succeeded"
    assert result.task_results[0].attempt_no == 2
    assert task.report_dates == [date(2026, 7, 27), date(2026, 7, 27)]
    assert delays == [1.0]


@pytest.mark.asyncio
async def test_job_029_worker_logs_service_and_retry_run_id() -> None:
    retry_run_id = uuid4()
    task = _Task(
        task_id="us.daily-bars",
        outcomes=[
            ScheduledTaskResult(
                task_id="us.daily-bars",
                provider_role="test.provider.primary",
                status="retryable",
                run_id=retry_run_id,
            ),
            None,
        ],
    )
    worker = ScheduledIngestionWorker(
        tasks=[task],
        report_date_lock=_Lock(),
        max_attempts=2,
        sleeper=lambda _: _record_delay([], 0),
    )

    with capture_logs() as logs:
        await worker.run_for_date(date(2026, 7, 27))

    relevant = [
        event
        for event in logs
        if event["event"]
        in {"scheduled_task_retrying", "scheduled_task_finished", "scheduled_report_finished"}
    ]
    assert relevant
    assert {event["service"] for event in relevant} == {"macro-data-worker"}
    retry_event = next(event for event in relevant if event["event"] == "scheduled_task_retrying")
    assert retry_event["run_id"] == str(retry_run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tasks", "acquired", "expected_status", "expected_error_code"),
    [
        ([], True, "blocked", "SCHEDULED_TASKS_NOT_CONFIGURED"),
        ([_Task(task_id="us.daily-bars")], False, "locked", "REPORT_DATE_LOCKED"),
    ],
)
async def test_job_029_worker_logs_early_terminal_report_outcomes(
    tasks: list[_Task],
    acquired: bool,
    expected_status: str,
    expected_error_code: str,
) -> None:
    worker = ScheduledIngestionWorker(tasks=tasks, report_date_lock=_Lock(acquired=acquired))

    with capture_logs() as logs:
        result = await worker.run_for_date(date(2026, 7, 27))

    assert result.status == expected_status
    event = next(event for event in logs if event["event"] == "scheduled_report_finished")
    assert event["service"] == "macro-data-worker"
    assert event["terminal"] == expected_status
    assert event["error_code"] == expected_error_code


@pytest.mark.asyncio
async def test_job_029_exhausted_required_retry_budget_stays_retryable() -> None:
    task = _Task(
        task_id="us.daily-bars",
        outcomes=[
            RetryableScheduledTaskError("rate limited"),
            RetryableScheduledTaskError("rate limited"),
        ],
    )
    worker = ScheduledIngestionWorker(tasks=[task], report_date_lock=_Lock(), max_attempts=2)

    result = await worker.run_for_date(date(2026, 7, 27))

    assert result.status == "retryable"
    assert result.task_results[0].status == "retryable"
    assert result.task_results[0].attempt_no == 2


@pytest.mark.asyncio
async def test_job_029_returned_retryable_result_uses_the_retry_budget() -> None:
    task = _Task(
        task_id="us.daily-bars",
        outcomes=[
            ScheduledTaskResult(
                task_id="us.daily-bars",
                provider_role="us.bars.primary",
                status="retryable",
                error_code="PROVIDER_RATE_LIMIT",
            ),
            None,
        ],
    )
    delays: list[float] = []
    worker = ScheduledIngestionWorker(
        tasks=[task],
        report_date_lock=_Lock(),
        max_attempts=2,
        sleeper=lambda delay: _record_delay(delays, delay),
    )

    result = await worker.run_for_date(date(2026, 7, 27))

    assert result.status == "succeeded"
    assert result.task_results[0].attempt_no == 2
    assert delays == [1.0]


@pytest.mark.asyncio
async def test_job_029_success_without_a_durable_run_id_fails_closed() -> None:
    task = _Task(
        task_id="us.daily-bars",
        outcomes=[
            ScheduledTaskResult(
                task_id="us.daily-bars",
                provider_role="us.bars.primary",
                status="succeeded",
            )
        ],
    )
    worker = ScheduledIngestionWorker(tasks=[task], report_date_lock=_Lock())

    result = await worker.run_for_date(date(2026, 7, 27))

    assert result.status == "blocked"
    assert result.task_results[0].error_code == "MISSING_DURABLE_RUN_ID"


@pytest.mark.asyncio
async def test_job_029_worker_degrades_for_optional_task_and_backfills_each_date() -> None:
    task = _Task(task_id="optional.news", required=False, outcomes=[RuntimeError("offline")])
    worker = ScheduledIngestionWorker(tasks=[task], report_date_lock=_Lock(), max_attempts=1)

    result = await worker.run_for_date(date(2026, 7, 27))
    backfill = await worker.backfill(date(2026, 7, 28), date(2026, 7, 29))

    assert result.status == "degraded"
    assert result.task_results[0].status == "failed"
    assert [item.report_date for item in backfill] == [date(2026, 7, 28), date(2026, 7, 29)]


@pytest.mark.asyncio
async def test_job_029_worker_skips_an_already_locked_report_date() -> None:
    task = _Task(task_id="cn.daily-bars")
    worker = ScheduledIngestionWorker(tasks=[task], report_date_lock=_Lock(acquired=False))

    result = await worker.run_for_date(date(2026, 7, 27))

    assert result.status == "locked"
    assert result.task_results == ()
    assert task.report_dates == []


@pytest.mark.asyncio
async def test_job_029_empty_task_bundle_fails_closed() -> None:
    worker = ScheduledIngestionWorker(tasks=[], report_date_lock=_Lock())

    result = await worker.run_for_date(date(2026, 7, 27))

    assert result.status == "blocked"
    assert result.task_results == ()


@pytest.mark.asyncio
async def test_job_029_worker_persists_materialized_gate_evidence_and_blocks_the_report() -> None:
    task = _Task(task_id="cn.daily-bars")

    class _Materializer:
        async def materialize(
            self,
            report_date: date,
            *,
            task_results: tuple[ScheduledTaskResult, ...],
        ) -> object:
            assert report_date == date(2026, 7, 27)
            assert task_results[0].run_id is not None
            return SimpleNamespace(
                snapshot=SimpleNamespace(snapshot_id="snapshot-job-029"),
                quality=SimpleNamespace(status="blocked"),
            )

    worker = ScheduledIngestionWorker(
        tasks=[task],
        report_date_lock=_Lock(),
        input_materializer=_Materializer(),  # type: ignore[arg-type]
    )

    result = await worker.run_for_date(date(2026, 7, 27))

    assert result.status == "blocked"
    assert result.snapshot_id == "snapshot-job-029"
    assert result.quality_status == "blocked"


@pytest.mark.asyncio
async def test_job_029_worker_entrypoint_fails_closed_without_registered_schedule() -> None:
    with pytest.raises(SchedulerNotConfiguredError, match="not configured"):
        await run_scheduler()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_status", ["retryable", "locked"])
async def test_job_029_configured_schedule_retries_nonterminal_report_dates(
    first_status: str,
) -> None:
    report_date = date(2026, 7, 27)
    clock = {"now": datetime(2026, 7, 27, 8, 0, tzinfo=UTC)}

    class _Worker:
        def __init__(self) -> None:
            self.report_dates: list[date] = []

        async def run_for_date(self, value: date) -> object:
            self.report_dates.append(value)
            status = first_status if len(self.report_dates) == 1 else "succeeded"
            return SimpleNamespace(status=status)

    async def advance_clock(delay: float) -> None:
        clock["now"] += timedelta(seconds=delay)

    worker = _Worker()
    await _run_configured_schedule(
        worker,  # type: ignore[arg-type]
        timezone=ZoneInfo("UTC"),
        hour=8,
        minute=0,
        poll_seconds=1,
        now=lambda: clock["now"],
        sleeper=advance_clock,
        retry_delay_seconds=1,
        max_report_attempts=2,
        max_schedule_runs=2,
    )

    assert worker.report_dates == [report_date, report_date]


@pytest.mark.asyncio
async def test_job_029_configured_schedule_does_not_retry_blocked_report_dates() -> None:
    clock = {"now": datetime(2026, 7, 27, 8, 0, tzinfo=UTC)}

    class _Worker:
        def __init__(self) -> None:
            self.report_dates: list[date] = []

        async def run_for_date(self, value: date) -> object:
            self.report_dates.append(value)
            return SimpleNamespace(status="blocked")

    async def advance_clock(delay: float) -> None:
        clock["now"] += timedelta(seconds=delay)

    worker = _Worker()
    await _run_configured_schedule(
        worker,  # type: ignore[arg-type]
        timezone=ZoneInfo("UTC"),
        hour=8,
        minute=0,
        poll_seconds=1,
        now=lambda: clock["now"],
        sleeper=advance_clock,
        retry_delay_seconds=1,
        max_report_attempts=3,
        max_schedule_runs=1,
    )

    assert worker.report_dates == [date(2026, 7, 27)]


@pytest.mark.asyncio
async def test_job_029_configured_schedule_stops_at_the_report_retry_budget() -> None:
    clock = {"now": datetime(2026, 7, 27, 8, 0, tzinfo=UTC)}

    class _Worker:
        def __init__(self) -> None:
            self.report_dates: list[date] = []

        async def run_for_date(self, value: date) -> object:
            self.report_dates.append(value)
            return SimpleNamespace(status="retryable")

    async def advance_clock(delay: float) -> None:
        clock["now"] += timedelta(seconds=delay)

    worker = _Worker()
    await _run_configured_schedule(
        worker,  # type: ignore[arg-type]
        timezone=ZoneInfo("UTC"),
        hour=8,
        minute=0,
        poll_seconds=1,
        now=lambda: clock["now"],
        sleeper=advance_clock,
        retry_delay_seconds=1,
        max_report_attempts=2,
        max_schedule_runs=2,
    )

    assert worker.report_dates == [date(2026, 7, 27), date(2026, 7, 27)]


async def _record_delay(delays: list[float], delay: float) -> None:
    delays.append(delay)
