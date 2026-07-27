from __future__ import annotations

from typing import Protocol, runtime_checkable

from macro_platform.contracts.provider import IngestJobRequest, IngestJobResult
from macro_platform.governance.source_policy import (
    PolicyPurpose,
    SourcePolicy,
    SourcePolicyDeniedError,
)
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

    def __init__(
        self,
        handler: IngestJobHandler,
        *,
        source_policy: SourcePolicy,
        database: Database | None = None,
    ) -> None:
        self._handler = handler
        self._database = database
        self._source_policy = source_policy

    async def execute(self, request: IngestJobRequest) -> IngestJobResult:
        self._require_ingestion_policy(request)
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

    def _require_ingestion_policy(self, request: IngestJobRequest) -> None:
        if not self._source_policy.production_enforced:
            return
        provider_id = getattr(self._handler, "provider_id", None)
        if not isinstance(provider_id, str) or not provider_id:
            raise ValueError("source-policy enforcement requires handler.provider_id")
        for region in request.regions:
            for purpose in (PolicyPurpose.INGESTION, PolicyPurpose.RETENTION):
                decision = self._source_policy.decision(
                    provider_id=provider_id,
                    dataset=request.dataset,
                    region=region,
                    purpose=purpose,
                )
                if not decision.allowed:
                    raise SourcePolicyDeniedError(decision)
