"""Public contracts shared by scheduled task, worker, and runtime modules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID

from macro_platform.contracts.common import Region
from macro_platform.contracts.provider import Dataset, IngestJobRequest, IngestJobResult
from macro_platform.storage.repositories import ScheduledTaskCheckpoint

if TYPE_CHECKING:
    from macro_platform.services.report_input_materializer import MaterializedReportInput

ScheduledTaskStatus = Literal["succeeded", "failed", "retryable"]
ScheduledWorkerStatus = Literal["succeeded", "degraded", "blocked", "retryable", "locked"]


@dataclass(frozen=True, slots=True)
class ScheduledTaskResult:
    task_id: str
    provider_role: str
    status: ScheduledTaskStatus
    dataset: Dataset | None = None
    region: Region | None = None
    record_count: int = 0
    records_rejected: int = 0
    attempt_no: int = 1
    run_id: UUID | None = None
    run_ids: tuple[UUID, ...] = ()
    error_code: str | None = None

    @property
    def evidence_run_ids(self) -> tuple[UUID, ...]:
        """All durable page runs whose committed records may support this task."""

        values = (*self.run_ids, *(() if self.run_id is None else (self.run_id,)))
        return tuple(dict.fromkeys(values))


@dataclass(frozen=True, slots=True)
class ScheduledWorkerResult:
    report_date: date
    status: ScheduledWorkerStatus
    task_results: tuple[ScheduledTaskResult, ...]
    snapshot_id: str | None = None
    quality_status: Literal["passed", "degraded", "blocked", "retryable"] | None = None


class ScheduledTask(Protocol):
    task_id: str
    required: bool

    async def run(self, report_date: date) -> ScheduledTaskResult: ...


class ReportInputMaterializer(Protocol):
    async def materialize(
        self,
        report_date: date,
        *,
        task_results: tuple[ScheduledTaskResult, ...],
    ) -> MaterializedReportInput: ...


class ScheduledTaskExecutor(Protocol):
    """The durable execution seam; production supplies ``JobRunner``."""

    async def execute(self, request: IngestJobRequest) -> IngestJobResult: ...


class ScheduledTaskCheckpointStore(Protocol):
    """State needed to continue a report-date task after worker restart."""

    async def begin_or_load(
        self,
        *,
        report_date: date,
        task_id: str,
        provider_role: str,
        dataset: Dataset,
        region: str,
        request_as_of: datetime,
        lease_owner_id: UUID,
    ) -> ScheduledTaskCheckpoint: ...

    async def advance(
        self,
        checkpoint: ScheduledTaskCheckpoint,
        *,
        run_id: UUID,
        next_cursor: str | None,
        source_watermark: str | None,
        records_accepted: int,
        records_rejected: int,
    ) -> ScheduledTaskCheckpoint: ...


ScheduledRequestFactory = Callable[[date, datetime, str | None], IngestJobRequest]
ScheduledSleeper = Callable[[float], Awaitable[None]]
