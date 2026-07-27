from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

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
from macro_platform.normalization.common import canonical_json_checksum
from macro_platform.storage.database import Database
from macro_platform.storage.repositories import IngestionRunRepository
from macro_platform.storage.unit_of_work import UnitOfWork


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


@runtime_checkable
class DurableRunAwareIngestJobHandler(IngestJobHandler, Protocol):
    """Production handlers receive their persisted run ID before writing audits."""

    def set_durable_run_id(self, run_id: UUID) -> None: ...


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
        durable_run_id, replay_result = await self._reserve_checkpointed_production_run(request)
        if replay_result is not None:
            return replay_result
        if durable_run_id is not None:
            if not isinstance(self._handler, DurableRunAwareIngestJobHandler):
                raise ValueError("production ingestion handler must accept a durable run ID")
            self._handler.set_durable_run_id(durable_run_id)
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
        if durable_run_id is not None:
            if result.run_id != durable_run_id:
                raise ValueError("production ingestion handler returned a different durable run ID")
            if self._database is None:
                raise ValueError("durable ingestion completion requires a database")
            async with UnitOfWork(self._database).transaction() as session:
                await IngestionRunRepository(session).complete_run(result)
        return result

    async def _reserve_checkpointed_production_run(
        self, request: IngestJobRequest
    ) -> tuple[UUID | None, IngestJobResult | None]:
        if not self._source_policy.production_enforced or not isinstance(
            self._handler, CheckpointedIngestJobHandler
        ):
            return None, None
        if self._database is None:
            raise ValueError("checkpointed production ingestion requires a database")
        idempotency_key = canonical_json_checksum(request.model_dump(mode="json"))
        async with UnitOfWork(self._database).transaction() as session:
            repository = IngestionRunRepository(session)
            run_id, created = await repository.reserve_run(request, idempotency_key=idempotency_key)
            if created:
                return run_id, None
            completed = await repository.load_completed_result(run_id)
        return run_id, completed

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
