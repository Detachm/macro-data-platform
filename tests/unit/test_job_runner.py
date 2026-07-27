from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from macro_platform.contracts.common import Region
from macro_platform.contracts.provider import Dataset, IngestJobRequest, IngestJobResult
from macro_platform.governance.source_policy import (
    ApprovalStatus,
    IngestionRetentionPolicy,
    NonProductionSourcePolicy,
    ProductionSourcePolicy,
    RetentionRule,
    SourcePolicyDeniedError,
    SourcePolicyEntry,
    SourcePolicyManifest,
)
from macro_platform.jobs import runner as runner_module
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


class _PolicyAwareHandler:
    provider_id = "pending.provider.v1"

    def __init__(self) -> None:
        self.was_run = False

    async def run(self, request: IngestJobRequest) -> IngestJobResult:
        self.was_run = True
        return _result(request)


class _RetentionAwareHandler(_PolicyAwareHandler):
    def __init__(self) -> None:
        super().__init__()
        self.retention_policy: IngestionRetentionPolicy | None = None

    def set_retention_policy(self, policy: IngestionRetentionPolicy) -> None:
        self.retention_policy = policy


class _WaitingCheckpointedHandler(_RetentionAwareHandler):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.run_id = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def set_durable_run_id(self, run_id: object) -> None:
        self.run_id = run_id

    async def run(self, request: IngestJobRequest) -> IngestJobResult:
        raise AssertionError("checkpointed handler must not use the non-durable path")

    async def run_checkpointed(
        self,
        request: IngestJobRequest,
        checkpoints: IngestionCheckpointService,
        database: Database,
    ) -> IngestJobResult:
        assert self.run_id is not None
        assert isinstance(checkpoints, IngestionCheckpointService)
        self.calls += 1
        self.started.set()
        await self.release.wait()
        result = _result(request)
        return result.model_copy(update={"run_id": self.run_id})


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

    async def reserve_run(
        self, request: IngestJobRequest, *, idempotency_key: str
    ) -> tuple[object, bool]:
        self.__class__.reservations += 1
        return self.run_id, self.__class__.reservations == 1

    async def load_completed_result(self, run_id: object) -> IngestJobResult | None:
        assert run_id == self.run_id
        return self.__class__.completed

    async def load_run(self, run_id: object) -> object:
        assert run_id == self.run_id
        return SimpleNamespace(status="running")

    async def complete_run(self, result: IngestJobResult) -> None:
        self.__class__.completed = result


def _approved_policy(*, retention_rule: RetentionRule) -> ProductionSourcePolicy:
    return ProductionSourcePolicy(
        SourcePolicyManifest(
            policy_version="test",
            entries=[
                SourcePolicyEntry(
                    policy_id="approved-ingestion",
                    provider_id="pending.provider.v1",
                    dataset=Dataset.INSTRUMENTS,
                    regions={Region.CN},
                    owner="@kazming666",
                    credential_requirement="none",
                    ingestion_allowed=True,
                    external_llm_allowed=True,
                    citation_allowed=True,
                    retention_rule=retention_rule,
                    approval_status=ApprovalStatus.APPROVED,
                    production_enabled=True,
                    evidence=["docs/data-sources/cn-hk-mvp.md"],
                )
            ],
        )
    )


async def test_job_runner_invokes_checkpointed_ingestion_path() -> None:
    database = Database("postgresql+asyncpg://macro:macro@127.0.0.1:5432/macro_data")
    handler = _CheckpointedHandler()
    try:
        assert (
            await JobRunner(
                handler,
                database=database,
                source_policy=NonProductionSourcePolicy(),
            ).execute(_request())
        ).status == "succeeded"
        assert handler.was_checkpointed
    finally:
        await database.dispose()


async def test_checkpointed_handler_requires_database() -> None:
    with pytest.raises(ValueError, match="requires a database"):
        await JobRunner(
            _CheckpointedHandler(),
            source_policy=NonProductionSourcePolicy(),
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
        source_policy=_approved_policy(retention_rule=RetentionRule.CANONICAL_FACTS),
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


async def test_gov_026_job_runner_rejects_unapproved_ingestion_before_handler_runs() -> None:
    policy = ProductionSourcePolicy(
        SourcePolicyManifest(
            policy_version="test",
            entries=[
                SourcePolicyEntry(
                    policy_id="pending-ingestion",
                    provider_id="pending.provider.v1",
                    dataset=Dataset.INSTRUMENTS,
                    regions={Region.CN},
                    owner="@kazming666",
                    credential_requirement="none",
                    ingestion_allowed=True,
                    external_llm_allowed=True,
                    citation_allowed=True,
                    retention_rule=RetentionRule.METADATA_ONLY,
                    approval_status=ApprovalStatus.PENDING,
                    production_enabled=False,
                    evidence=["docs/data-sources/cn-hk-mvp.md"],
                )
            ],
        )
    )
    handler = _PolicyAwareHandler()

    with pytest.raises(SourcePolicyDeniedError, match="approval status is pending"):
        await JobRunner(handler, source_policy=policy).execute(_request())

    assert not handler.was_run


async def test_gov_026_job_runner_rejects_sources_without_retention_permission() -> None:
    policy = ProductionSourcePolicy(
        SourcePolicyManifest(
            policy_version="test",
            entries=[
                SourcePolicyEntry(
                    policy_id="retention-denied-ingestion",
                    provider_id="pending.provider.v1",
                    dataset=Dataset.INSTRUMENTS,
                    regions={Region.CN},
                    owner="@kazming666",
                    credential_requirement="none",
                    ingestion_allowed=True,
                    external_llm_allowed=True,
                    citation_allowed=True,
                    retention_rule=RetentionRule.NONE,
                    approval_status=ApprovalStatus.APPROVED,
                    production_enabled=True,
                    evidence=["docs/data-sources/cn-hk-mvp.md"],
                )
            ],
        )
    )
    handler = _PolicyAwareHandler()

    with pytest.raises(SourcePolicyDeniedError, match="retention is not allowed"):
        await JobRunner(handler, source_policy=policy).execute(_request())

    assert not handler.was_run


async def test_gov_026_job_runner_passes_metadata_only_to_the_record_writer() -> None:
    handler = _RetentionAwareHandler()

    assert (
        await JobRunner(
            handler,
            source_policy=_approved_policy(retention_rule=RetentionRule.METADATA_ONLY),
        ).execute(_request())
    ).status == "succeeded"
    assert handler.retention_policy is not None
    assert handler.retention_policy.rule_for(Region.CN) is RetentionRule.METADATA_ONLY


async def test_gov_026_job_runner_rejects_production_handlers_without_retention_enforcement() -> (
    None
):
    handler = _PolicyAwareHandler()

    with pytest.raises(ValueError, match="must enforce the retention policy"):
        await JobRunner(
            handler,
            source_policy=_approved_policy(retention_rule=RetentionRule.CANONICAL_FACTS),
        ).execute(_request())

    assert not handler.was_run
