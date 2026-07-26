from __future__ import annotations

from typing import Protocol, runtime_checkable

from macro_platform.contracts.provider import IngestJobRequest, IngestJobResult
from macro_platform.jobs.ingestion_checkpoint import IngestionCheckpointService
from macro_platform.storage.database import Database


class IngestJobHandler(Protocol):
    async def run(self, request: IngestJobRequest) -> IngestJobResult: ...


@runtime_checkable
class CheckpointedIngestJobHandler(IngestJobHandler, Protocol):
    """Actual ingest handler path for durable page/audit lifecycle operations."""

    async def run_checkpointed(
        self,
        request: IngestJobRequest,
        checkpoints: IngestionCheckpointService,
        database: Database,
    ) -> IngestJobResult: ...


class JobRunner:
    """Execution seam for retries, locks, checkpoints, and metrics."""

    def __init__(self, handler: IngestJobHandler, *, database: Database | None = None) -> None:
        self._handler = handler
        self._database = database

    async def execute(self, request: IngestJobRequest) -> IngestJobResult:
        if isinstance(self._handler, CheckpointedIngestJobHandler):
            if self._database is None:
                raise ValueError("checkpointed ingestion requires a database")
            result = await self._handler.run_checkpointed(
                request, IngestionCheckpointService(), self._database
            )
        else:
            result = await self._handler.run(request)
        if result.provider_role != request.provider_role or result.dataset != request.dataset:
            raise ValueError("job result does not match its request")
        return result
