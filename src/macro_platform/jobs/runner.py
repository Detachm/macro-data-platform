from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from macro_platform.contracts.provider import IngestJobRequest, IngestJobResult
from macro_platform.jobs.ingestion_checkpoint import IngestionCheckpointService
from macro_platform.normalization.common import canonical_json_checksum, utc_now
from macro_platform.storage.database import Database
from macro_platform.storage.repositories import IngestionRunLease, IngestionRunRepository
from macro_platform.storage.unit_of_work import UnitOfWork


class IngestJobHandler(Protocol):
    async def run(self, request: IngestJobRequest) -> IngestJobResult: ...


@dataclass(frozen=True, slots=True)
class IngestionExecutionContext:
    """Immutable execution identity passed to a checkpointed handler."""

    run_id: UUID


@runtime_checkable
class CheckpointedIngestJobHandler(IngestJobHandler, Protocol):
    """Actual ingest handler path for durable page/audit lifecycle operations."""

    async def run_checkpointed(
        self,
        request: IngestJobRequest,
        checkpoints: IngestionCheckpointService,
        database: Database,
        execution: IngestionExecutionContext,
    ) -> IngestJobResult: ...


class IngestionRunInProgressError(RuntimeError):
    """An equivalent durable run is still owned by another worker."""


class JobRunner:
    """Execution seam for retries, locks, checkpoints, and metrics."""

    def __init__(
        self,
        handler: IngestJobHandler,
        *,
        database: Database | None = None,
        running_run_wait_seconds: float = 30.0,
        running_run_poll_seconds: float = 0.1,
        run_lease_seconds: float = 60.0,
        now: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if running_run_wait_seconds <= 0 or running_run_poll_seconds <= 0 or run_lease_seconds <= 0:
            raise ValueError("running-run wait and poll durations must be positive")
        self._handler = handler
        self._database = database
        self._id_factory = id_factory
        self._monotonic = monotonic
        self._now = now
        self._running_run_wait_seconds = running_run_wait_seconds
        self._running_run_poll_seconds = running_run_poll_seconds
        self._run_lease_seconds = run_lease_seconds
        self._sleeper = sleeper

    async def execute(self, request: IngestJobRequest) -> IngestJobResult:
        checkpointed_handler = (
            self._handler if isinstance(self._handler, CheckpointedIngestJobHandler) else None
        )
        run_lease, replay_result = await self._acquire_checkpointed_run(request)
        if replay_result is not None:
            return replay_result
        heartbeat = (
            None
            if run_lease is None
            else asyncio.create_task(self._renew_lease_until_finished(run_lease))
        )
        try:
            if checkpointed_handler is not None:
                if self._database is None:
                    raise ValueError("checkpointed ingestion requires a database")
                if run_lease is None:
                    raise RuntimeError("checkpointed ingestion requires a durable run lease")
                result = await checkpointed_handler.run_checkpointed(
                    request,
                    IngestionCheckpointService(),
                    self._database,
                    IngestionExecutionContext(
                        run_id=run_lease.run_id,
                    ),
                )
            else:
                result = await self._handler.run(request)
            if result.provider_role != request.provider_role or result.dataset != request.dataset:
                raise ValueError("job result does not match its request")
            if run_lease is not None:
                if result.run_id != run_lease.run_id:
                    raise ValueError("durable ingestion handler returned a different run ID")
                if self._database is None:
                    raise ValueError("durable ingestion completion requires a database")
                async with UnitOfWork(self._database).transaction() as session:
                    completed = await IngestionRunRepository(session).complete_run(
                        result, lease=run_lease
                    )
                if not completed:
                    raise IngestionRunInProgressError(
                        "durable ingestion run lease was lost before completion"
                    )
            return result
        except Exception as error:
            if run_lease is not None:
                await self._fail_durable_run(run_lease, error)
            raise
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat

    async def _acquire_checkpointed_run(
        self, request: IngestJobRequest
    ) -> tuple[IngestionRunLease | None, IngestJobResult | None]:
        if not isinstance(self._handler, CheckpointedIngestJobHandler):
            return None, None
        if self._database is None:
            raise ValueError("durable checkpointed ingestion requires a database")
        idempotency_key = canonical_json_checksum(request.model_dump(mode="json"))
        async with UnitOfWork(self._database).transaction() as session:
            repository = IngestionRunRepository(session)
            now = self._now()
            lease, run_id = await repository.acquire_run(
                request,
                idempotency_key=idempotency_key,
                run_id=self._id_factory(),
                now=now,
                lease_expires_at=now + timedelta(seconds=self._run_lease_seconds),
            )
            if lease is not None:
                return lease, None
            completed = await repository.load_completed_result(run_id)
        if completed is not None:
            return None, completed
        return await self._wait_or_reclaim_run(run_id)

    async def _wait_or_reclaim_run(
        self, run_id: UUID
    ) -> tuple[IngestionRunLease | None, IngestJobResult | None]:
        """Wait for a live owner or fence an expired/crashed worker before recovery."""

        if self._database is None:
            raise ValueError("durable checkpointed ingestion requires a database")
        deadline = self._monotonic() + self._running_run_wait_seconds
        while True:
            async with UnitOfWork(self._database).transaction() as session:
                repository = IngestionRunRepository(session)
                completed = await repository.load_completed_result(run_id)
                if completed is not None:
                    return None, completed
                now = self._now()
                lease = await repository.claim_recoverable_run(
                    run_id,
                    now=now,
                    lease_expires_at=now + timedelta(seconds=self._run_lease_seconds),
                )
                if lease is not None:
                    return lease, None
                run = await repository.load_run(run_id)
            if run is None:
                raise IngestionRunInProgressError(
                    "durable ingestion run disappeared before recovery"
                )
            if run.status != "running":
                raise IngestionRunInProgressError(
                    f"durable ingestion run finished without a replayable result: {run.status}"
                )
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise IngestionRunInProgressError(
                    f"durable ingestion run {run_id} is still running; "
                    "retry after its owner finishes"
                )
            await self._sleeper(min(self._running_run_poll_seconds, remaining))

    async def _renew_lease_until_finished(self, lease: IngestionRunLease) -> None:
        if self._database is None:
            raise ValueError("durable checkpointed ingestion requires a database")
        while True:
            await self._sleeper(self._run_lease_seconds / 3)
            now = self._now()
            async with UnitOfWork(self._database).transaction() as session:
                renewed = await IngestionRunRepository(session).renew_lease(
                    lease, lease_expires_at=now + timedelta(seconds=self._run_lease_seconds)
                )
            if not renewed:
                return

    async def _fail_durable_run(self, lease: IngestionRunLease, error: Exception) -> None:
        if self._database is None:
            return
        async with UnitOfWork(self._database).transaction() as session:
            await IngestionRunRepository(session).fail_run(
                lease, error_code=type(error).__name__.upper()
            )
