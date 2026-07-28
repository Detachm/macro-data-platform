"""Durable, page-aware adapters from scheduled tasks to ``JobRunner``."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from macro_platform.contracts.common import Region
from macro_platform.contracts.provider import Dataset
from macro_platform.jobs.scheduled_types import (
    ScheduledRequestFactory,
    ScheduledTaskCheckpointStore,
    ScheduledTaskExecutor,
    ScheduledTaskResult,
)
from macro_platform.providers.base import ProviderError
from macro_platform.storage.database import Database
from macro_platform.storage.repositories import (
    ScheduledTaskCheckpoint,
    ScheduledTaskCheckpointRepository,
)
from macro_platform.storage.unit_of_work import UnitOfWork


class PostgresScheduledTaskCheckpointStore:
    """Transactional adapter around the durable scheduler checkpoint repository."""

    def __init__(self, database: Database) -> None:
        self._database = database

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
    ) -> ScheduledTaskCheckpoint:
        async with UnitOfWork(self._database).transaction() as session:
            return await ScheduledTaskCheckpointRepository(session).begin_or_load(
                report_date=report_date,
                task_id=task_id,
                provider_role=provider_role,
                dataset=dataset,
                region=region,
                request_as_of=request_as_of,
                lease_owner_id=lease_owner_id,
            )

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
        async with UnitOfWork(self._database).transaction() as session:
            return await ScheduledTaskCheckpointRepository(session).advance(
                checkpoint,
                run_id=run_id,
                next_cursor=next_cursor,
                source_watermark=source_watermark,
                records_accepted=records_accepted,
                records_rejected=records_rejected,
            )


class CheckpointedScheduledTask:
    """Run every provider page through ``JobRunner`` with a fenced checkpoint."""

    def __init__(
        self,
        *,
        task_id: str,
        required: bool,
        provider_role: str,
        dataset: Dataset,
        region: Region,
        executor: ScheduledTaskExecutor,
        checkpoint_store: ScheduledTaskCheckpointStore,
        request_factory: ScheduledRequestFactory,
        now: Callable[[], datetime],
        request_as_of_lead: timedelta = timedelta(days=1),
    ) -> None:
        if not task_id.strip() or not provider_role.strip():
            raise ValueError("scheduled task and provider role must not be empty")
        if request_as_of_lead <= timedelta(0):
            raise ValueError("scheduled task request_as_of_lead must be positive")
        self.task_id = task_id
        self.required = required
        self.provider_role = provider_role
        self._dataset = dataset
        self._region = region
        self._executor = executor
        self._checkpoint_store = checkpoint_store
        self._request_factory = request_factory
        self._now = now
        self._request_as_of_lead = request_as_of_lead

    async def run(self, report_date: date) -> ScheduledTaskResult:
        checkpoint = await self._checkpoint_store.begin_or_load(
            report_date=report_date,
            task_id=self.task_id,
            provider_role=self.provider_role,
            dataset=self._dataset,
            region=self._region.value,
            request_as_of=self._now().astimezone(UTC) + self._request_as_of_lead,
            lease_owner_id=uuid4(),
        )
        if checkpoint.status == "completed":
            return scheduled_task_result_from_checkpoint(checkpoint)
        while True:
            request = self._request_factory(
                report_date,
                checkpoint.request_as_of,
                checkpoint.next_cursor,
            )
            try:
                result = await self._executor.execute(request)
            except ProviderError as error:
                return ScheduledTaskResult(
                    task_id=self.task_id,
                    provider_role=self.provider_role,
                    dataset=self._dataset,
                    region=self._region,
                    status="retryable" if error.retryable else "failed",
                    error_code=error.code,
                )
            if result.provider_role != self.provider_role or result.dataset is not self._dataset:
                raise ValueError("scheduled task received a mismatched durable ingestion result")
            if result.status == "retry_wait":
                return ScheduledTaskResult(
                    task_id=self.task_id,
                    provider_role=self.provider_role,
                    dataset=self._dataset,
                    region=self._region,
                    status="retryable",
                    run_id=result.run_id,
                    error_code=result.error_code,
                )
            if result.status == "failed":
                return ScheduledTaskResult(
                    task_id=self.task_id,
                    provider_role=self.provider_role,
                    dataset=self._dataset,
                    region=self._region,
                    status="failed",
                    run_id=result.run_id,
                    error_code=result.error_code,
                )
            checkpoint = await self._checkpoint_store.advance(
                checkpoint,
                run_id=result.run_id,
                next_cursor=result.next_cursor,
                source_watermark=result.source_watermark,
                records_accepted=result.records_accepted,
                records_rejected=result.records_rejected,
            )
            if checkpoint.status == "completed":
                return scheduled_task_result_from_checkpoint(checkpoint)


def scheduled_task_result_from_checkpoint(
    checkpoint: ScheduledTaskCheckpoint,
) -> ScheduledTaskResult:
    if checkpoint.run_id is None:
        raise RuntimeError("completed scheduled task checkpoint is missing its durable run ID")
    return ScheduledTaskResult(
        task_id=checkpoint.task_id,
        provider_role=checkpoint.provider_role,
        dataset=checkpoint.dataset,
        region=Region(checkpoint.region),
        status="succeeded",
        record_count=checkpoint.records_accepted,
        records_rejected=checkpoint.records_rejected,
        run_id=checkpoint.run_id,
    )
