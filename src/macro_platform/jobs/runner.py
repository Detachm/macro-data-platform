from __future__ import annotations

from typing import Protocol, runtime_checkable

from macro_platform.contracts.common import Region
from macro_platform.contracts.provider import IngestJobRequest, IngestJobResult
from macro_platform.governance.source_policy import (
    IngestionRetentionPolicy,
    PolicyPurpose,
    RetentionRule,
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


@runtime_checkable
class RetentionAwareIngestJobHandler(IngestJobHandler, Protocol):
    """A production handler must accept policy before it can write records."""

    def set_retention_policy(self, policy: IngestionRetentionPolicy) -> None: ...


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
        retention_policy = self._require_ingestion_policy(request)
        if retention_policy is not None:
            if not isinstance(self._handler, RetentionAwareIngestJobHandler):
                raise ValueError("production ingestion handler must enforce the retention policy")
            self._handler.set_retention_policy(retention_policy)
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

    def _require_ingestion_policy(
        self, request: IngestJobRequest
    ) -> IngestionRetentionPolicy | None:
        if not self._source_policy.production_enforced:
            return None
        provider_id = getattr(self._handler, "provider_id", None)
        if not isinstance(provider_id, str) or not provider_id:
            raise ValueError("source-policy enforcement requires handler.provider_id")
        rules_by_region: dict[Region, RetentionRule] = {}
        for region in request.regions:
            ingestion = self._source_policy.decision(
                provider_id=provider_id,
                dataset=request.dataset,
                region=region,
                purpose=PolicyPurpose.INGESTION,
            )
            if not ingestion.allowed:
                raise SourcePolicyDeniedError(ingestion)
            retention = self._source_policy.decision(
                provider_id=provider_id,
                dataset=request.dataset,
                region=region,
                purpose=PolicyPurpose.RETENTION,
            )
            if not retention.allowed:
                raise SourcePolicyDeniedError(retention)
            if retention.retention_rule is None:
                raise ValueError("allowed retention policy must provide a retention rule")
            rules_by_region[region] = retention.retention_rule
        return IngestionRetentionPolicy(rules_by_region=rules_by_region)
