from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from macro_platform.contracts.common import Region
from macro_platform.contracts.provider import Dataset, IngestJobRequest, IngestJobResult
from macro_platform.jobs.ingestion_checkpoint import IngestionCheckpointService
from macro_platform.jobs.runner import JobRunner
from macro_platform.storage.database import Database


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
    ) -> IngestJobResult:
        assert isinstance(checkpoints, IngestionCheckpointService)
        assert isinstance(database, Database)
        self.was_checkpointed = True
        return _result(request)


async def test_job_runner_invokes_checkpointed_ingestion_path() -> None:
    database = Database("postgresql+asyncpg://macro:macro@127.0.0.1:5432/macro_data")
    handler = _CheckpointedHandler()
    try:
        assert (
            await JobRunner(handler, database=database).execute(_request())
        ).status == "succeeded"
        assert handler.was_checkpointed
    finally:
        await database.dispose()


async def test_checkpointed_handler_requires_database() -> None:
    with pytest.raises(ValueError, match="requires a database"):
        await JobRunner(_CheckpointedHandler()).execute(_request())
