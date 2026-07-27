from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from macro_platform.contracts.common import Region
from macro_platform.contracts.provider import Dataset, IngestJobRequest, IngestJobResult
from macro_platform.jobs import runner as runner_module
from macro_platform.jobs.ingestion_checkpoint import IngestionCheckpointService
from macro_platform.jobs.runner import IngestionExecutionContext, JobRunner
from macro_platform.storage.database import Database
from macro_platform.storage.repositories import IngestionRunLease


def _request() -> IngestJobRequest:
    return IngestJobRequest(
        provider_role="cn.contract_fixture.instruments",
        dataset=Dataset.INSTRUMENTS,
        regions={Region.CN},
        start=datetime(2026, 7, 24, tzinfo=UTC),
        end=datetime(2026, 7, 25, tzinfo=UTC),
        as_of=datetime(2026, 7, 25, tzinfo=UTC),
    )


def _result(request: IngestJobRequest) -> IngestJobResult:
    return IngestJobResult(
        run_id=uuid4(),
        status="succeeded",
        provider_role=request.provider_role,
        dataset=request.dataset,
        started_at=request.start,
        finished_at=request.end,
        records_fetched=0,
        records_accepted=0,
        records_rejected=0,
        records_inserted=0,
        records_updated=0,
    )


class _CheckpointedHandler:
    def __init__(self) -> None:
        self.was_checkpointed = False

    async def run(self, request: IngestJobRequest) -> IngestJobResult:
        raise AssertionError("checkpointed handlers must use the checkpointed runner path")

    async def run_checkpointed(
        self,
        request: IngestJobRequest,
        checkpoints: IngestionCheckpointService,
        database: Database,
        execution: IngestionExecutionContext,
    ) -> IngestJobResult:
        assert isinstance(checkpoints, IngestionCheckpointService)
        assert isinstance(database, Database)
        self.was_checkpointed = True
        return _result(request).model_copy(update={"run_id": execution.run_id})


class _WaitingCheckpointedHandler:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, request: IngestJobRequest) -> IngestJobResult:
        raise AssertionError("checkpointed handler must not use the non-durable path")

    async def run_checkpointed(
        self,
        request: IngestJobRequest,
        checkpoints: IngestionCheckpointService,
        database: Database,
        execution: IngestionExecutionContext,
    ) -> IngestJobResult:
        assert isinstance(checkpoints, IngestionCheckpointService)
        self.calls += 1
        self.started.set()
        await self.release.wait()
        result = _result(request)
        return result.model_copy(update={"run_id": execution.run_id})


class _FakeUnitOfWork:
    def __init__(self, database: object) -> None:
        self._database = database

    @asynccontextmanager
    async def transaction(self):
        yield object()


class _WaitingRunRepository:
    run_id = uuid4()
    completed: IngestJobResult | None = None
    reservations = 0

    def __init__(self, session: object) -> None:
        self._session = session

    async def acquire_run(
        self,
        request: IngestJobRequest,
        *,
        idempotency_key: str,
        run_id: object,
        now: object,
        lease_expires_at: object,
    ) -> tuple[IngestionRunLease | None, object]:
        self.__class__.reservations += 1
        if self.__class__.reservations == 1:
            return IngestionRunLease(run_id=self.run_id, attempt_no=1), self.run_id
        return None, self.run_id

    async def load_completed_result(self, run_id: object) -> IngestJobResult | None:
        assert run_id == self.run_id
        return self.__class__.completed

    async def load_run(self, run_id: object) -> object:
        assert run_id == self.run_id
        return SimpleNamespace(status="running")

    async def claim_recoverable_run(
        self, run_id: object, *, now: object, lease_expires_at: object
    ) -> IngestionRunLease | None:
        assert run_id == self.run_id
        return None

    async def renew_lease(self, lease: IngestionRunLease, *, lease_expires_at: object) -> bool:
        return lease.run_id == self.run_id

    async def complete_run(self, result: IngestJobResult, *, lease: IngestionRunLease) -> bool:
        assert lease.run_id == self.run_id
        self.__class__.completed = result
        return True

    async def fail_run(self, lease: IngestionRunLease, *, error_code: str) -> bool:
        assert lease.run_id == self.run_id
        return True


async def test_job_runner_invokes_checkpointed_ingestion_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _WaitingRunRepository.run_id = uuid4()
    _WaitingRunRepository.completed = None
    _WaitingRunRepository.reservations = 0
    monkeypatch.setattr(runner_module, "UnitOfWork", _FakeUnitOfWork)
    monkeypatch.setattr(runner_module, "IngestionRunRepository", _WaitingRunRepository)
    database = Database("postgresql+asyncpg://macro:macro@127.0.0.1:5432/macro_data")
    handler = _CheckpointedHandler()
    try:
        assert (
            await JobRunner(
                handler,
                database=database,
            ).execute(_request())
        ).status == "succeeded"
        assert handler.was_checkpointed
    finally:
        await database.dispose()


async def test_checkpointed_handler_requires_database() -> None:
    with pytest.raises(ValueError, match="requires a database"):
        await JobRunner(
            _CheckpointedHandler(),
        ).execute(_request())


async def test_job_runner_waits_for_the_active_durable_run_before_replaying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _WaitingRunRepository.run_id = uuid4()
    _WaitingRunRepository.completed = None
    _WaitingRunRepository.reservations = 0
    monkeypatch.setattr(runner_module, "UnitOfWork", _FakeUnitOfWork)
    monkeypatch.setattr(runner_module, "IngestionRunRepository", _WaitingRunRepository)
    handler = _WaitingCheckpointedHandler()
    runner = JobRunner(
        handler,
        database=object(),  # type: ignore[arg-type]
        running_run_wait_seconds=1,
        running_run_poll_seconds=0.001,
    )
    request = _request()
    first_task = asyncio.create_task(runner.execute(request))
    await asyncio.wait_for(handler.started.wait(), timeout=1)
    second_task = asyncio.create_task(runner.execute(request))

    try:
        await asyncio.sleep(0.01)
        assert handler.calls == 1
        handler.release.set()
        first = await first_task
        second = await second_task
    finally:
        handler.release.set()
        await asyncio.gather(first_task, second_task, return_exceptions=True)

    assert first == second
    assert handler.calls == 1
