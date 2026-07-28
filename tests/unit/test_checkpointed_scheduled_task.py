from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from macro_platform.contracts.common import Region
from macro_platform.contracts.provider import Dataset, IngestJobRequest, IngestJobResult
from macro_platform.jobs.scheduler import CheckpointedScheduledTask, ScheduledTaskResult
from macro_platform.storage.repositories import ScheduledTaskCheckpoint

NOW = datetime(2026, 7, 28, 0, 0, tzinfo=UTC)
REPORT_DATE = date(2026, 7, 28)


def _request(cursor: str | None, request_as_of: datetime) -> IngestJobRequest:
    return IngestJobRequest(
        provider_role="us.market.primary",
        dataset=Dataset.BARS,
        regions={Region.US},
        start=datetime(2026, 7, 20, tzinfo=UTC),
        end=datetime(2026, 7, 28, tzinfo=UTC),
        as_of=request_as_of,
        cursor=cursor,
    )


def _result(request: IngestJobRequest, *, cursor: str | None, run_id: UUID) -> IngestJobResult:
    return IngestJobResult(
        run_id=run_id,
        status="succeeded",
        provider_role=request.provider_role,
        dataset=request.dataset,
        started_at=NOW,
        finished_at=NOW,
        records_fetched=3,
        records_accepted=3,
        records_rejected=0,
        records_inserted=3,
        records_updated=0,
        next_cursor=cursor,
        source_watermark="watermark-1",
    )


@dataclass
class _Executor:
    results: list[IngestJobResult]
    requests: list[IngestJobRequest]

    async def execute(self, request: IngestJobRequest) -> IngestJobResult:
        self.requests.append(request)
        return self.results.pop(0)


@dataclass
class _CheckpointStore:
    checkpoint: ScheduledTaskCheckpoint
    advances: list[tuple[str | None, UUID]]

    async def begin_or_load(
        self,
        *,
        report_date: date,
        task_id: str,
        provider_role: str,
        dataset: Dataset,
        region: str,
        request_as_of: datetime,
    ) -> ScheduledTaskCheckpoint:
        assert (report_date, task_id, provider_role, dataset, region) == (
            REPORT_DATE,
            "us.daily-bars",
            "us.market.primary",
            Dataset.BARS,
            Region.US.value,
        )
        return self.checkpoint

    async def advance(
        self,
        checkpoint: ScheduledTaskCheckpoint,
        *,
        run_id: UUID,
        next_cursor: str | None,
        source_watermark: str | None,
        records_accepted: int,
        records_rejected: int,
    ) -> ScheduledTaskCheckpoint:
        assert source_watermark == "watermark-1"
        assert records_accepted == 3
        assert records_rejected == 0
        self.advances.append((next_cursor, run_id))
        self.checkpoint = ScheduledTaskCheckpoint(
            **{
                **checkpoint.__dict__,
                "status": "completed" if next_cursor is None else "active",
                "next_cursor": next_cursor,
                "source_watermark": source_watermark,
                "run_id": run_id,
                "records_accepted": checkpoint.records_accepted + records_accepted,
            }
        )
        return self.checkpoint


def _checkpoint(*, cursor: str | None = None, completed: bool = False) -> ScheduledTaskCheckpoint:
    return ScheduledTaskCheckpoint(
        report_date=REPORT_DATE,
        task_id="us.daily-bars",
        provider_role="us.market.primary",
        dataset=Dataset.BARS,
        region=Region.US.value,
        request_as_of=NOW,
        status="completed" if completed else "active",
        next_cursor=cursor,
        source_watermark=None,
        run_id=None,
        records_accepted=0,
        records_rejected=0,
    )


@pytest.mark.asyncio
async def test_job_029_checkpointed_task_resumes_the_persisted_cursor_until_completion() -> None:
    first_run_id = uuid4()
    second_run_id = uuid4()
    executor = _Executor(
        results=[
            _result(_request("cursor-1", NOW), cursor=None, run_id=second_run_id),
        ],
        requests=[],
    )
    checkpoint_store = _CheckpointStore(_checkpoint(cursor="cursor-1"), [])
    task = CheckpointedScheduledTask(
        task_id="us.daily-bars",
        required=True,
        provider_role="us.market.primary",
        dataset=Dataset.BARS,
        region=Region.US,
        executor=executor,
        checkpoint_store=checkpoint_store,
        request_factory=lambda _date, as_of, cursor: _request(cursor, as_of),
        now=lambda: NOW,
    )

    result = await task.run(REPORT_DATE)

    assert result == ScheduledTaskResult(
        task_id="us.daily-bars",
        provider_role="us.market.primary",
        dataset=Dataset.BARS,
        region=Region.US,
        status="succeeded",
        record_count=3,
        run_id=second_run_id,
    )
    assert [request.cursor for request in executor.requests] == ["cursor-1"]
    assert checkpoint_store.advances == [(None, second_run_id)]
    assert first_run_id != second_run_id


@pytest.mark.asyncio
async def test_job_029_completed_task_replays_its_durable_result_without_upstream_execution() -> (
    None
):
    run_id = uuid4()
    checkpoint_store = _CheckpointStore(
        ScheduledTaskCheckpoint(
            **{**_checkpoint(completed=True).__dict__, "run_id": run_id, "records_accepted": 9}
        ),
        [],
    )
    executor = _Executor(results=[], requests=[])
    task = CheckpointedScheduledTask(
        task_id="us.daily-bars",
        required=True,
        provider_role="us.market.primary",
        dataset=Dataset.BARS,
        region=Region.US,
        executor=executor,
        checkpoint_store=checkpoint_store,
        request_factory=lambda _date, as_of, cursor: _request(cursor, as_of),
        now=lambda: NOW,
    )

    result = await task.run(REPORT_DATE)

    assert result.status == "succeeded"
    assert result.run_id == run_id
    assert result.record_count == 9
    assert executor.requests == []
