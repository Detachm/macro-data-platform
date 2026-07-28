from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from docker.errors import DockerException
from sqlalchemy import func, select, text
from testcontainers.postgres import PostgresContainer

from macro_platform.contracts.common import AssetClass, AvailabilityBasis, Region, SourceRef
from macro_platform.contracts.macro import (
    Frequency,
    MacroObservation,
    MacroObservationQuery,
    MacroRelease,
    MacroReleaseQuery,
    MacroSeries,
)
from macro_platform.contracts.market import (
    Adjustment,
    BarQuery,
    Instrument,
    InstrumentQuery,
    InstrumentStatus,
    Interval,
    MarketBar,
    MarketObservation,
    MarketObservationQuery,
    MarketSnapshotQuery,
    ScopeType,
)
from macro_platform.contracts.news import ContentMode, NewsQuery
from macro_platform.contracts.provider import Dataset, IngestJobRequest, IngestJobResult
from macro_platform.contracts.report import ReportValidationIssue
from macro_platform.jobs.ingestion_checkpoint import CommittedPage, IngestionCheckpointService
from macro_platform.jobs.runner import IngestionExecutionContext, JobRunner
from macro_platform.storage.database import Database
from macro_platform.storage.models import (
    DailyReportRow,
    DailyReportSourceRefRow,
    DeliveryAttemptRow,
    IngestPageCommitRow,
    InstrumentRow,
    MacroReleaseRevisionRow,
    MarketBarRevisionRow,
    MarketBarRow,
    MarketObservationRow,
    ProviderRunRow,
    ReportGenerationAttemptRow,
    ReportInputSnapshotRow,
)
from macro_platform.storage.reporting import (
    DeliveryAttempt,
    ReportGenerationAttempt,
    ReportInputSnapshot,
    StoredDailyReport,
)
from macro_platform.storage.repositories import (
    IngestionCheckpointRepository,
    IngestionRunRepository,
    NormalizedFactRepository,
    PostgresDataRepository,
    ReportRepository,
)
from macro_platform.storage.unit_of_work import UnitOfWork
from tests.helpers import CHECKSUM, NOW, news_event

pytestmark = pytest.mark.integration

_previous_schema_run_id: UUID | None = None
_previous_schema_instrument_id: str | None = None


async def _seed_0002_records(database_url: str, run_id: UUID, instrument_id: str) -> None:
    database = Database(database_url)
    try:
        async with database.session() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO provider_runs "
                    "(run_id, provider_role, dataset, status, started_at, details) "
                    "VALUES (:run_id, :provider_role, :dataset, :status, :started_at, "
                    "CAST(:details AS jsonb))"
                ),
                {
                    "run_id": run_id,
                    "provider_role": "migration.0002.preservation",
                    "dataset": Dataset.MARKET_OBSERVATIONS.value,
                    "status": "succeeded",
                    "started_at": NOW,
                    "details": "{}",
                },
            )
            await session.execute(
                text(
                    "INSERT INTO instruments "
                    "(instrument_id, canonical_symbol, region, status, valid_from, payload) "
                    "VALUES (:instrument_id, :canonical_symbol, :region, :status, :valid_from, "
                    "CAST(:payload AS jsonb))"
                ),
                {
                    "instrument_id": instrument_id,
                    "canonical_symbol": f"XSHG:{instrument_id}",
                    "region": Region.CN.value,
                    "status": "active",
                    "valid_from": date(2020, 1, 1),
                    "payload": json.dumps(
                        {
                            "source": {
                                "provider_id": "migration.0002.provider",
                                "provider_record_id": "legacy-instrument-record",
                                "checksum_sha256": "a" * 64,
                                "retrieved_at": NOW.isoformat(),
                            }
                        }
                    ),
                },
            )
    finally:
        await database.dispose()


@pytest.fixture(scope="module")
def postgresql_url() -> Iterator[str]:
    """Exercise an upgrade from the prior released schema before repository tests."""

    supplied_url = os.environ.get("CONTRACT_TEST_DATABASE_URL")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    global _previous_schema_instrument_id, _previous_schema_run_id
    _previous_schema_run_id = uuid4()
    _previous_schema_instrument_id = f"legacy-instrument-{uuid4().hex}"
    if supplied_url is not None:
        config.attributes["database_url"] = supplied_url
        command.upgrade(config, "0002")
        asyncio.run(
            _seed_0002_records(
                supplied_url, _previous_schema_run_id, _previous_schema_instrument_id
            )
        )
        command.upgrade(config, "head")
        yield supplied_url
        return
    try:
        with PostgresContainer(
            "postgres:16-alpine", username="macro", password="macro", dbname="macro_data_test"
        ) as postgres:
            database_url = postgres.get_connection_url().replace("psycopg2", "asyncpg")
            config.attributes["database_url"] = database_url
            command.upgrade(config, "0002")
            asyncio.run(
                _seed_0002_records(
                    database_url, _previous_schema_run_id, _previous_schema_instrument_id
                )
            )
            command.upgrade(config, "head")
            yield database_url
    except DockerException as error:
        pytest.skip(f"Docker is unavailable for PostgreSQL repository contracts: {error}")


@pytest.fixture
async def database(postgresql_url: str) -> AsyncIterator[Database]:
    value = Database(postgresql_url)
    if not await value.ready():
        await value.dispose()
        pytest.fail("testcontainers PostgreSQL did not become ready")
    try:
        yield value
    finally:
        await value.dispose()


def _source(record_id: str, *, checksum: str = CHECKSUM) -> SourceRef:
    return SourceRef(
        provider_id="storage.contract.provider.v1",
        provider_record_id=record_id,
        source_name="Storage contract fixture",
        source_url=f"https://example.com/storage/{record_id}",
        retrieved_at=NOW,
        checksum_sha256=checksum,
    )


def _report_commands(token: str) -> tuple[ReportInputSnapshot, StoredDailyReport, DeliveryAttempt]:
    report = json.loads(
        (Path(__file__).resolve().parents[1] / "golden" / "daily_report_v1_success.json").read_text(
            encoding="utf-8"
        )
    )
    report_id = f"daily-report-{token}"
    snapshot_id = f"snapshot-{token}"
    fingerprint = token * 2
    report["report_id"] = report_id
    report["input_snapshot"] = {
        **report["input_snapshot"],
        "snapshot_id": snapshot_id,
        "fingerprint_sha256": fingerprint,
    }
    snapshot_payload = report["input_snapshot"]
    snapshot = ReportInputSnapshot(
        snapshot_id=snapshot_id,
        snapshot_version=snapshot_payload["snapshot_version"],
        report_date=report["report_date"],
        as_of=snapshot_payload["as_of"],
        cutoff_at=snapshot_payload["cutoff_at"],
        fingerprint_sha256=fingerprint,
        fact_ids=snapshot_payload["fact_ids"],
        payload=snapshot_payload,
    )
    stored_report = StoredDailyReport(
        report_id=report_id,
        report_date=report["report_date"],
        report_version=f"v1-{token}",
        contract_version=report["contract_version"],
        input_snapshot_id=snapshot_id,
        status=report["status"],
        publication_decision=report["publication"]["decision"],
        generated_at=report["generated_at"],
        payload=report,
    )
    return (
        snapshot,
        stored_report,
        DeliveryAttempt(
            delivery_id=uuid4(),
            report_id=report_id,
            report_version=stored_report.report_version,
            delivery_target="feishu:contract-test",
            idempotency_key=f"delivery-{token}",
            request_payload={"report_id": report_id},
        ),
    )


def _ingest_request() -> IngestJobRequest:
    return IngestJobRequest(
        provider_role="cn.contract_fixture.market_observations",
        dataset=Dataset.MARKET_OBSERVATIONS,
        regions={Region.CN},
        start=NOW - timedelta(hours=1),
        end=NOW,
        as_of=NOW,
    )


class _ProductionCheckpointedHandler:
    provider_id = "storage.contract.provider.v1"

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, request: IngestJobRequest) -> IngestJobResult:
        raise AssertionError("checkpointed handler must use the durable runner path")

    async def run_checkpointed(
        self,
        request: IngestJobRequest,
        checkpoints: IngestionCheckpointService,
        database: Database,
        execution: IngestionExecutionContext,
    ) -> IngestJobResult:
        assert isinstance(checkpoints, IngestionCheckpointService)
        assert isinstance(database, Database)
        self.calls += 1
        return IngestJobResult(
            run_id=execution.run_id,
            status="succeeded",
            provider_role=request.provider_role,
            dataset=request.dataset,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
            records_fetched=1,
            records_accepted=1,
            records_rejected=0,
            records_inserted=1,
            records_updated=0,
        )


class _BlockingProductionCheckpointedHandler(_ProductionCheckpointedHandler):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.all_started = asyncio.Event()
        self.release = asyncio.Event()
        self.executions: list[IngestionExecutionContext] = []

    async def run_checkpointed(
        self,
        request: IngestJobRequest,
        checkpoints: IngestionCheckpointService,
        database: Database,
        execution: IngestionExecutionContext,
    ) -> IngestJobResult:
        assert isinstance(checkpoints, IngestionCheckpointService)
        assert isinstance(database, Database)
        self.calls += 1
        self.executions.append(execution)
        self.started.set()
        if self.calls == 2:
            self.all_started.set()
        await self.release.wait()
        return IngestJobResult(
            run_id=execution.run_id,
            status="succeeded",
            provider_role=request.provider_role,
            dataset=request.dataset,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
            records_fetched=1,
            records_accepted=1,
            records_rejected=0,
            records_inserted=1,
            records_updated=0,
        )


class _WrongRunProductionCheckpointedHandler(_ProductionCheckpointedHandler):
    def __init__(self) -> None:
        super().__init__()
        self.execution: IngestionExecutionContext | None = None

    async def run_checkpointed(
        self,
        request: IngestJobRequest,
        checkpoints: IngestionCheckpointService,
        database: Database,
        execution: IngestionExecutionContext,
    ) -> IngestJobResult:
        self.execution = execution
        result = await super().run_checkpointed(request, checkpoints, database, execution)
        return result.model_copy(update={"run_id": uuid4()})


async def test_db_002_migration_upgrades_0002_to_current_schema(database: Database) -> None:
    async with database.session() as session:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        tables = set(
            (
                await session.scalars(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = 'public' AND tablename IN "
                        "('report_input_snapshots', 'daily_reports', 'daily_report_source_refs', "
                        "'delivery_attempts', 'macro_release_revisions', 'market_bar_revisions', "
                        "'report_generation_attempts', 'scheduled_task_checkpoints')"
                    )
                )
            ).all()
        )
        preserved_run = await session.get(ProviderRunRow, _previous_schema_run_id)
        preserved_instrument = await session.get(InstrumentRow, _previous_schema_instrument_id)
    assert revision == "0011"
    assert tables == {
        "report_input_snapshots",
        "daily_reports",
        "daily_report_source_refs",
        "delivery_attempts",
        "macro_release_revisions",
        "market_bar_revisions",
        "report_generation_attempts",
        "scheduled_task_checkpoints",
    }
    assert preserved_run is not None
    assert preserved_run.idempotency_key is None
    assert preserved_run.request_payload == {}
    assert preserved_instrument is not None
    assert preserved_instrument.ingestion_run_id is None
    assert (
        preserved_instrument.payload["source"]["provider_record_id"] == "legacy-instrument-record"
    )


async def test_rpt_030_generation_attempt_is_auditable_and_recoverable(
    database: Database,
) -> None:
    token = uuid4().hex
    snapshot, report, _ = _report_commands(token)
    generation_id = uuid4()
    attempt = ReportGenerationAttempt(
        generation_id=generation_id,
        report_id=report.report_id,
        report_version=report.report_version,
        input_snapshot_id=snapshot.snapshot_id,
        prompt_version="daily-report-v1.0",
        model="contract-test-model",
        model_parameters={"temperature": 0},
        input_fingerprint_sha256=snapshot.fingerprint_sha256,
        source_ref_ids=[],
    )

    async with UnitOfWork(database).transaction() as session:
        repository = ReportRepository(session)
        assert await repository.put_input_snapshot(snapshot)
        assert await repository.put_generation_attempt(attempt)
        assert not await repository.put_generation_attempt(attempt)
        generated_attempt = attempt.model_copy(
            update={
                "lifecycle_status": "generated",
                "response_payload": {"contract_name": "DailyReport"},
            }
        )
        await repository.update_generation_attempt(generated_attempt)
        assert await repository.put_report(
            report.model_copy(update={"generation_id": generation_id})
        )

    async with database.session() as session:
        recovered_attempt = await ReportRepository(session).load_generation_attempt(generation_id)
        row = await session.get(ReportGenerationAttemptRow, generation_id)

    assert recovered_attempt == generated_attempt
    assert row is not None
    assert row.lifecycle_status == "generated"


async def test_rpt_031_validation_errors_round_trip_with_report(
    database: Database,
) -> None:
    token = uuid4().hex
    snapshot, report, _ = _report_commands(token)
    issue = ReportValidationIssue(
        code="FACT_VALUE_MISMATCH",
        message="test validation error",
        fact_id="fact.market.cn.index.csi300.change_pct",
    )
    report = report.model_copy(update={"validation_errors": [issue]})

    async with UnitOfWork(database).transaction() as session:
        repository = ReportRepository(session)
        assert await repository.put_input_snapshot(snapshot)
        assert await repository.put_report(report)
        validated = report.model_copy(update={"lifecycle_status": "validated"})
        assert await repository.update_report_validation(
            validated,
            expected_lifecycle_status="generated",
        )
        report = validated

    async with database.session() as session:
        recovered = await ReportRepository(session).load_report(report.report_id)

    assert recovered == report


async def test_rep_027_001_report_and_delivery_replays_are_idempotent(
    database: Database,
) -> None:
    token = uuid4().hex
    snapshot, report, delivery = _report_commands(token)

    async with UnitOfWork(database).transaction() as session:
        repository = ReportRepository(session)
        assert await repository.put_input_snapshot(snapshot)
        assert await repository.put_report(report)
        assert await repository.reserve_delivery_attempt(delivery)
        await repository.update_delivery_attempt(
            delivery_id=delivery.delivery_id,
            expected_attempt_no=1,
            status="failed",
            response_payload={"error": "timeout after write"},
        )
        assert await repository.retry_delivery_attempt(delivery.delivery_id)
        assert not await repository.update_delivery_attempt(
            delivery_id=delivery.delivery_id,
            expected_attempt_no=1,
            status="succeeded",
            response_payload={"message_id": "stale-worker"},
        )

    async with UnitOfWork(database).transaction() as session:
        repository = ReportRepository(session)
        assert not await repository.put_input_snapshot(snapshot)
        assert not await repository.put_report(report)
        with pytest.raises(ValueError, match="snapshot is immutable"):
            await repository.put_input_snapshot(
                snapshot.model_copy(update={"report_date": date(2026, 7, 25)})
            )
        with pytest.raises(ValueError, match="daily report is immutable"):
            await repository.put_report(report.model_copy(update={"report_version": "v2"}))
        replay = delivery.model_copy(update={"delivery_id": uuid4()})
        assert not await repository.reserve_delivery_attempt(replay)
        recovered_delivery = await repository.load_delivery_attempt_for_key(
            report_id=delivery.report_id,
            delivery_target=delivery.delivery_target,
            idempotency_key=delivery.idempotency_key,
        )
        assert recovered_delivery is not None
        assert recovered_delivery.status == "pending"
        assert recovered_delivery.attempt_no == 2

        conflicting_payload = {**report.payload, "report_id": f"{report.report_id}-conflict"}
        conflicting_report = report.model_copy(
            update={"report_id": f"{report.report_id}-conflict", "payload": conflicting_payload}
        )
        with pytest.raises(ValueError, match="date/version"):
            await repository.put_report(conflicting_report)

    async with database.session() as session:
        snapshot_count = await session.scalar(
            select(func.count())
            .select_from(ReportInputSnapshotRow)
            .where(ReportInputSnapshotRow.snapshot_id == snapshot.snapshot_id)
        )
        report_count = await session.scalar(
            select(func.count())
            .select_from(DailyReportRow)
            .where(DailyReportRow.report_id == report.report_id)
        )
        delivery_count = await session.scalar(
            select(func.count())
            .select_from(DeliveryAttemptRow)
            .where(DeliveryAttemptRow.report_id == report.report_id)
        )
        source_ref_count = await session.scalar(
            select(func.count())
            .select_from(DailyReportSourceRefRow)
            .where(DailyReportSourceRefRow.report_id == report.report_id)
        )
    assert (snapshot_count, report_count, delivery_count) == (1, 1, 1)
    assert source_ref_count == len(report.source_references())


async def test_rep_027_incomplete_report_with_no_source_references_is_persisted(
    database: Database,
) -> None:
    token = uuid4().hex
    snapshot, complete_report, _ = _report_commands(token)
    incomplete_payload = json.loads(json.dumps(complete_report.payload))
    incomplete_payload["status"] = "incomplete"
    incomplete_payload["publication"]["decision"] = "not_published"
    incomplete_payload["sections"]["source_references"]["items"] = []

    def remove_source_reference_ids(value: object) -> None:
        if isinstance(value, dict):
            value.pop("source_ref_ids", None)
            for child in value.values():
                remove_source_reference_ids(child)
        elif isinstance(value, list):
            for child in value:
                remove_source_reference_ids(child)

    remove_source_reference_ids(incomplete_payload["sections"])
    incomplete_report = StoredDailyReport(
        report_id=complete_report.report_id,
        report_date=complete_report.report_date,
        report_version=complete_report.report_version,
        contract_version=complete_report.contract_version,
        input_snapshot_id=complete_report.input_snapshot_id,
        status="incomplete",
        publication_decision="not_published",
        generated_at=complete_report.generated_at,
        payload=incomplete_payload,
    )

    async with UnitOfWork(database).transaction() as session:
        repository = ReportRepository(session)
        assert await repository.put_input_snapshot(snapshot)
        assert await repository.put_report(incomplete_report)

    async with database.session() as session:
        source_ref_count = await session.scalar(
            select(func.count())
            .select_from(DailyReportSourceRefRow)
            .where(DailyReportSourceRefRow.report_id == incomplete_report.report_id)
        )
    assert source_ref_count == 0


async def test_rep_027_rejects_report_references_outside_its_snapshot_and_source_index(
    database: Database,
) -> None:
    token = uuid4().hex
    snapshot, report, _ = _report_commands(token)

    unknown_source_payload = json.loads(json.dumps(report.payload))
    unknown_source_payload["sections"]["upcoming_calendar"]["items"][0]["source_ref_ids"] = [
        "source.unknown"
    ]
    with pytest.raises(ValueError, match="source_ref_ids"):
        StoredDailyReport.model_validate(
            {**report.model_dump(mode="python"), "payload": unknown_source_payload}
        )

    unknown_fact_payload = json.loads(json.dumps(report.payload))
    unknown_fact_payload["sections"]["key_movements"]["items"][0]["fact_ids"] = ["fact.unknown"]
    unknown_fact_report = StoredDailyReport.model_validate(
        {**report.model_dump(mode="python"), "payload": unknown_fact_payload}
    )
    async with UnitOfWork(database).transaction() as session:
        repository = ReportRepository(session)
        assert await repository.put_input_snapshot(snapshot)
        with pytest.raises(ValueError, match="fact_ids"):
            await repository.put_report(unknown_fact_report)


async def test_rep_027_002_ingestion_run_and_report_recover_after_restart(
    database: Database, postgresql_url: str
) -> None:
    token = uuid4().hex
    request = _ingest_request()
    snapshot, report, delivery = _report_commands(token)

    async with UnitOfWork(database).transaction() as session:
        lease, run_id = await IngestionRunRepository(session).acquire_run(
            request,
            idempotency_key=f"ingest-{token}",
            run_id=uuid4(),
            now=NOW,
            lease_expires_at=NOW + timedelta(seconds=1),
        )
        assert lease is not None
        report_repository = ReportRepository(session)
        assert await report_repository.put_input_snapshot(snapshot)
        assert await report_repository.put_report(report)
        assert await report_repository.reserve_delivery_attempt(delivery)

    # Simulate a worker restart before it can acknowledge the running ingestion.
    async with UnitOfWork(database).transaction() as session:
        run_repository = IngestionRunRepository(session)
        replay_lease, replay_run_id = await run_repository.acquire_run(
            request,
            idempotency_key=f"ingest-{token}",
            run_id=uuid4(),
            now=NOW,
            lease_expires_at=NOW + timedelta(seconds=1),
        )
        assert replay_lease is None
        assert replay_run_id == run_id
        persisted = await run_repository.load_run(run_id)
        assert persisted is not None
        assert persisted.status == "running"
        recovered_lease = await run_repository.claim_recoverable_run(
            run_id,
            now=NOW + timedelta(seconds=2),
            lease_expires_at=NOW + timedelta(seconds=3),
        )
        assert recovered_lease is not None
        assert await run_repository.complete_run(
            IngestJobResult(
                run_id=run_id,
                status="succeeded",
                provider_role=request.provider_role,
                dataset=request.dataset,
                started_at=NOW,
                finished_at=NOW + timedelta(seconds=1),
                records_fetched=1,
                records_accepted=1,
                records_rejected=0,
                records_inserted=1,
                records_updated=0,
            ),
            lease=recovered_lease,
        )

    restarted = Database(postgresql_url)
    try:
        async with restarted.session() as session:
            run = await IngestionRunRepository(session).load_run(run_id)
            recovered_snapshot = await ReportRepository(session).load_input_snapshot(
                snapshot.snapshot_id
            )
            recovered_report = await ReportRepository(session).load_report(report.report_id)
            recovered_delivery = await ReportRepository(session).load_delivery_attempt(
                delivery.delivery_id
            )
    finally:
        await restarted.dispose()

    assert run is not None
    assert run.status == "succeeded"
    assert recovered_snapshot == snapshot
    assert recovered_report == report
    assert recovered_delivery == delivery


async def test_rpt_032_delivery_attempt_persists_feishu_audit_fields(
    database: Database,
) -> None:
    token = uuid4().hex
    snapshot, report, delivery = _report_commands(token)

    async with UnitOfWork(database).transaction() as session:
        repository = ReportRepository(session)
        assert await repository.put_input_snapshot(snapshot)
        assert await repository.put_report(report)
        assert await repository.reserve_delivery_attempt(delivery)
        assert await repository.update_delivery_attempt(
            delivery_id=delivery.delivery_id,
            expected_attempt_no=1,
            status="succeeded",
            response_payload={"provider": "feishu", "result": "succeeded"},
            message_id="om_contract_message",
        )

    async with database.session() as session:
        persisted = await ReportRepository(session).load_delivery_attempt(delivery.delivery_id)

    assert persisted is not None
    assert persisted.report_version == report.report_version
    assert persisted.status == "succeeded"
    assert persisted.message_id == "om_contract_message"
    assert persisted.error_code is None


async def test_rep_027_production_runner_replays_completed_ingestion_without_provider_call(
    database: Database,
) -> None:
    handler = _ProductionCheckpointedHandler()
    runner = JobRunner(handler, database=database)

    first = await runner.execute(_ingest_request())
    second = await runner.execute(_ingest_request())

    assert first == second
    assert handler.calls == 1


async def test_rep_027_production_runner_waits_for_an_active_idempotent_run(
    database: Database,
) -> None:
    handler = _BlockingProductionCheckpointedHandler()
    runner = JobRunner(
        handler,
        database=database,
        running_run_wait_seconds=1,
        running_run_poll_seconds=0.01,
    )
    request = _ingest_request().model_copy(
        update={"provider_role": "cn.contract_fixture.active_run_wait"}
    )
    first_task = asyncio.create_task(runner.execute(request))
    await asyncio.wait_for(handler.started.wait(), timeout=1)
    second_task = asyncio.create_task(runner.execute(request))

    try:
        await asyncio.sleep(0.05)
        assert handler.calls == 1
        handler.release.set()
        first = await first_task
        second = await second_task
    finally:
        handler.release.set()
        await asyncio.gather(first_task, second_task, return_exceptions=True)

    assert first == second
    assert handler.calls == 1


async def test_rep_027_production_runner_isolates_concurrent_execution_contexts(
    database: Database,
) -> None:
    handler = _BlockingProductionCheckpointedHandler()
    runner = JobRunner(
        handler,
        database=database,
        running_run_wait_seconds=1,
        running_run_poll_seconds=0.01,
    )
    first_request = _ingest_request().model_copy(
        update={"provider_role": "cn.contract_fixture.concurrent_first"}
    )
    second_request = first_request.model_copy(update={"cursor": "page-2"})
    first_task = asyncio.create_task(runner.execute(first_request))
    await asyncio.wait_for(handler.started.wait(), timeout=1)
    second_task = asyncio.create_task(runner.execute(second_request))

    try:
        await asyncio.wait_for(handler.all_started.wait(), timeout=1)
        assert handler.calls == 2
        handler.release.set()
        first, second = await asyncio.gather(first_task, second_task)
    finally:
        handler.release.set()
        await asyncio.gather(first_task, second_task, return_exceptions=True)

    assert first.run_id != second.run_id
    assert {execution.run_id for execution in handler.executions} == {first.run_id, second.run_id}


async def test_rep_027_marks_durable_run_failed_when_result_validation_fails(
    database: Database,
) -> None:
    handler = _WrongRunProductionCheckpointedHandler()
    runner = JobRunner(handler, database=database)
    request = _ingest_request().model_copy(
        update={"provider_role": f"cn.contract.invalid.{uuid4().hex[:8]}"}
    )

    with pytest.raises(ValueError, match="different run ID"):
        await runner.execute(request)

    assert handler.execution is not None
    async with database.session() as session:
        run = await session.get(ProviderRunRow, handler.execution.run_id)
    assert run is not None
    assert run.status == "failed"


async def test_db_004_concurrent_page_replay_commits_one_fact_and_checkpoint(
    database: Database,
) -> None:
    token = uuid4().hex
    observation = MarketObservation(
        observation_id=f"concurrent-observation-{token}",
        region=Region.CN,
        scope_type=ScopeType.MARKET,
        scope_id="CN",
        metric_code="market.storage_turnover",
        value=Decimal("1"),
        unit="CNY",
        period_start=NOW - timedelta(hours=1),
        period_end=NOW,
        observed_at=NOW,
        available_at=NOW,
        availability_basis=AvailabilityBasis.FIRST_SEEN,
        source=_source(f"concurrent-observation-{token}"),
    )
    page = CommittedPage(
        provider_role=f"cn.contract_fixture.concurrent.{token}",
        dataset=Dataset.MARKET_OBSERVATIONS,
        region=Region.CN.value,
        page_fingerprint="c" * 64,
        source_watermark="2026-07-23T08:00:00Z",
        next_cursor=None,
        accepted_record_ids=[observation.source.provider_record_id],
    )
    checkpoints = IngestionCheckpointService()
    request = _ingest_request().model_copy(
        update={"provider_role": page.provider_role, "dataset": page.dataset}
    )
    async with UnitOfWork(database).transaction() as session:
        lease, _ = await IngestionRunRepository(session).acquire_run(
            request,
            idempotency_key=f"concurrent-run-{token}",
            run_id=uuid4(),
            now=NOW,
            lease_expires_at=NOW + timedelta(minutes=1),
        )
    assert lease is not None

    async def commit_once() -> bool:
        async with UnitOfWork(database).transaction() as session:
            repository = IngestionCheckpointRepository(session, ingestion_run_id=lease.run_id)

            async def write_records(_: object) -> None:
                await repository.upsert_market_observation(observation)

            return await checkpoints.commit_page(repository, page, write_records)

    assert sorted(await asyncio.gather(commit_once(), commit_once())) == [False, True]
    async with database.session() as session:
        fact_count = await session.scalar(
            select(func.count())
            .select_from(MarketObservationRow)
            .where(MarketObservationRow.observation_id == observation.observation_id)
        )
        checkpoint = await session.scalar(
            select(IngestPageCommitRow).where(
                IngestPageCommitRow.provider_role == page.provider_role,
                IngestPageCommitRow.page_fingerprint == page.page_fingerprint,
            )
        )
    assert fact_count == 1
    assert checkpoint is not None


async def test_postgres_repository_persists_facts_pit_revisions_and_raw_source_fields(
    database: Database,
) -> None:
    token = uuid4().hex
    instrument = Instrument(
        instrument_id=f"ins-{token}",
        canonical_symbol="XSHG:600000",
        region=Region.CN,
        venue_mic="XSHG",
        local_symbol="600000",
        name="存储测试标的",
        asset_class=AssetClass.EQUITY,
        currency="CNY",
        timezone="Asia/Shanghai",
        status=InstrumentStatus.ACTIVE,
        valid_from=date(2020, 1, 1),
        source=_source(f"instrument-{token}"),
    )
    bar = MarketBar(
        bar_id=f"bar-{token}",
        instrument_id=instrument.instrument_id,
        canonical_symbol=instrument.canonical_symbol,
        region=Region.CN,
        interval=Interval.D1,
        bar_start=NOW - timedelta(days=1),
        bar_end=NOW,
        trading_date=(NOW - timedelta(days=1)).date(),
        open=Decimal("10.00"),
        high=Decimal("11.00"),
        low=Decimal("9.50"),
        close=Decimal("10.50"),
        volume=Decimal("100"),
        currency="CNY",
        adjustment=Adjustment.RAW,
        available_at=NOW,
        availability_basis=AvailabilityBasis.EXCHANGE_PUBLISHED,
        source=_source(f"bar-{token}"),
    )
    market_observation = MarketObservation(
        observation_id=f"market-observation-{token}",
        region=Region.CN,
        scope_type=ScopeType.MARKET,
        scope_id="CN",
        metric_code="market.turnover",
        value=Decimal("100.00"),
        unit="CNY",
        period_start=NOW - timedelta(hours=1),
        period_end=NOW,
        observed_at=NOW,
        available_at=NOW,
        availability_basis=AvailabilityBasis.FIRST_SEEN,
        source=_source(f"market-observation-{token}"),
    )
    series = MacroSeries(
        series_id=f"macro:CN:TEST:{token}",
        region=Region.CN,
        authority="Storage test authority",
        code=f"TEST-{token}",
        name="Storage test series",
        frequency=Frequency.MONTHLY,
        unit="index",
        transformation="level",
        seasonal_adjustment="not_adjusted",
        source=_source(f"series-{token}"),
    )
    first_vintage = MacroObservation(
        observation_id=f"macro-observation-first-{token}",
        series_id=series.series_id,
        region=Region.CN,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        value=Decimal("100"),
        unit="index",
        transformation="level",
        released_at=NOW,
        available_at=NOW,
        availability_basis=AvailabilityBasis.PROVIDER_DISSEMINATED,
        vintage_id="v1",
        revision_no=0,
        value_status="preliminary",
        source=_source(f"macro-observation-first-{token}"),
    )
    second_vintage = first_vintage.model_copy(
        update={
            "observation_id": f"macro-observation-second-{token}",
            "value": Decimal("98"),
            "available_at": NOW + timedelta(hours=2),
            "vintage_id": "v2",
            "revision_no": 1,
            "value_status": "final",
            "supersedes_observation_id": first_vintage.observation_id,
            "source": _source(f"macro-observation-second-{token}"),
        }
    )
    release = MacroRelease(
        release_id=f"macro-release-{token}",
        series_id=series.series_id,
        region=Region.CN,
        release_name="Storage test release",
        scheduled_at=NOW + timedelta(days=1),
        available_at=NOW,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        unit="index",
        status="scheduled",
        source=_source(f"macro-release-{token}"),
    )
    released = release.model_copy(
        update={
            "released_at": NOW + timedelta(hours=2),
            "available_at": NOW + timedelta(hours=2),
            "actual": Decimal("101"),
            "status": "released",
            "source": _source(f"macro-release-revised-{token}", checksum="b" * 64),
        }
    )
    event = news_event().model_copy(
        update={
            "news_id": f"news-{token}",
            "source": _source(f"news-{token}"),
            "published_at": NOW,
            "available_at": NOW,
        }
    )

    async with UnitOfWork(database).transaction() as session:
        run_id = uuid4()
        session.add(
            ProviderRunRow(
                run_id=run_id,
                idempotency_key=f"fact-run-{token}",
                provider_role="storage.contract.facts",
                dataset=Dataset.MACRO_RELEASES.value,
                status="succeeded",
                started_at=NOW,
                finished_at=NOW,
                records_fetched=0,
                records_accepted=0,
                records_rejected=0,
                details={},
                request_payload={},
            )
        )
        await session.flush()
        repository = NormalizedFactRepository(session, ingestion_run_id=run_id)
        await repository.upsert_instrument(instrument)
        await repository.upsert_bar(bar)
        await repository.upsert_market_observation(market_observation)
        await repository.upsert_macro_series(series)
        await repository.upsert_macro_observation(first_vintage)
        await repository.upsert_macro_observation(
            first_vintage.model_copy(update={"value": Decimal("999")})
        )
        await repository.upsert_macro_observation(second_vintage)
        await repository.upsert_macro_release(release)
        await repository.upsert_macro_release(released)
        await repository.upsert_news_event(event)

    repository = PostgresDataRepository(database)
    as_of_before_revision = NOW + timedelta(hours=1)
    assert await repository.list_instruments(
        InstrumentQuery(regions={Region.CN}, asset_classes={AssetClass.EQUITY})
    ) == [instrument]
    assert await repository.list_bars(
        BarQuery(
            instrument_ids=[instrument.instrument_id],
            interval=Interval.D1,
            start=NOW - timedelta(days=1),
            end=NOW + timedelta(seconds=1),
            as_of=as_of_before_revision,
        )
    ) == [bar]
    assert (
        await repository.list_snapshots(
            MarketSnapshotQuery(
                instrument_ids=[instrument.instrument_id], as_of=as_of_before_revision
            )
        )
    )[0].last == Decimal("10.50")
    assert await repository.list_market_observations(
        MarketObservationQuery(
            regions={Region.CN},
            metric_codes=[market_observation.metric_code],
            start=NOW - timedelta(hours=1),
            end=NOW + timedelta(seconds=1),
            as_of=as_of_before_revision,
        )
    ) == [market_observation]
    assert (
        await repository.list_macro_observations(
            MacroObservationQuery(
                series_ids=[series.series_id],
                period_from=first_vintage.period_end,
                period_to=first_vintage.period_end,
                as_of=as_of_before_revision,
            )
        )
    )[0].value == Decimal("100")
    assert (
        await repository.list_macro_observations(
            MacroObservationQuery(
                series_ids=[series.series_id],
                period_from=first_vintage.period_end,
                period_to=first_vintage.period_end,
                as_of=NOW + timedelta(hours=3),
            )
        )
    )[0].value == Decimal("98")
    assert await repository.list_macro_releases(
        MacroReleaseQuery(
            regions={Region.CN},
            scheduled_from=NOW,
            scheduled_to=NOW + timedelta(days=2),
            as_of=as_of_before_revision,
        )
    ) == [release]
    assert await repository.list_macro_releases(
        MacroReleaseQuery(
            regions={Region.CN},
            scheduled_from=NOW,
            scheduled_to=NOW + timedelta(days=2),
            as_of=NOW + timedelta(hours=3),
        )
    ) == [released]
    headlines = await repository.list_news(
        NewsQuery(
            regions={Region.CN},
            published_from=NOW - timedelta(hours=1),
            published_to=NOW + timedelta(hours=1),
            as_of=as_of_before_revision,
            content_mode=ContentMode.HEADLINE,
        )
    )
    assert len(headlines) == 1
    assert headlines[0].news_id == event.news_id
    assert headlines[0].summary is None
    assert headlines[0].body is None

    async with database.session() as session:
        source_lookup = await session.scalar(
            select(MarketBarRow.bar_id).where(
                MarketBarRow.payload["source"]["checksum_sha256"].as_string() == CHECKSUM
            )
        )
        revision_payload = await session.scalar(
            select(text("payload ->> 'value'"))
            .select_from(text("macro_observations"))
            .where(text("observation_id = :observation_id")),
            {"observation_id": first_vintage.observation_id},
        )
        fact_run_id = await session.scalar(
            select(MarketBarRow.ingestion_run_id).where(MarketBarRow.bar_id == bar.bar_id)
        )
    assert source_lookup == bar.bar_id
    assert revision_payload == "100"
    assert fact_run_id == run_id


async def test_STO_027_market_bar_revisions_preserve_first_seen_and_pit_history(
    database: Database,
) -> None:
    token = uuid4().hex
    run_id = uuid4()
    instrument = Instrument(
        instrument_id=f"ins-bar-revision-{token}",
        canonical_symbol=f"XSHG:BAR{token[:8].upper()}",
        region=Region.CN,
        venue_mic="XSHG",
        local_symbol=f"BAR{token[:8].upper()}",
        name="Market bar revision test",
        asset_class=AssetClass.EQUITY,
        currency="CNY",
        timezone="Asia/Shanghai",
        status=InstrumentStatus.ACTIVE,
        valid_from=date(2020, 1, 1),
        source=_source(f"bar-revision-instrument-{token}"),
    )
    base = MarketBar(
        bar_id=f"bar-revision-{token}",
        instrument_id=instrument.instrument_id,
        canonical_symbol=instrument.canonical_symbol,
        region=Region.CN,
        interval=Interval.D1,
        bar_start=NOW - timedelta(days=1),
        bar_end=NOW,
        trading_date=(NOW - timedelta(days=1)).date(),
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10.50"),
        volume=Decimal("100"),
        currency="CNY",
        adjustment=Adjustment.RAW,
        available_at=NOW,
        availability_basis=AvailabilityBasis.FIRST_SEEN,
        source=_source(f"bar-revision-base-{token}", checksum="a" * 64),
    )
    revised = base.model_copy(
        update={
            "available_at": NOW + timedelta(hours=1),
            "close": Decimal("10.75"),
            "source": _source(f"bar-revision-revised-{token}", checksum="b" * 64),
        }
    )
    async with UnitOfWork(database).transaction() as session:
        session.add(
            ProviderRunRow(
                run_id=run_id,
                idempotency_key=f"bar-revision-run-{token}",
                provider_role="storage.contract.bar_revisions",
                dataset=Dataset.BARS.value,
                status="succeeded",
                started_at=NOW,
                finished_at=NOW,
                records_fetched=2,
                records_accepted=2,
                records_rejected=0,
                details={},
                request_payload={},
            )
        )
        await session.flush()
        repository = NormalizedFactRepository(session, ingestion_run_id=run_id)
        await repository.upsert_instrument(instrument)
        await repository.upsert_bar(base)
        await repository.upsert_bar(revised)
        await repository.upsert_bar(revised)
        restored = base.model_copy(
            update={
                "available_at": NOW + timedelta(hours=2),
                "source": _source(f"bar-revision-restored-{token}", checksum="a" * 64),
            }
        )
        await repository.upsert_bar(restored)

    repository = PostgresDataRepository(database)
    query = BarQuery(
        instrument_ids=[instrument.instrument_id],
        interval=Interval.D1,
        start=NOW - timedelta(days=2),
        end=NOW + timedelta(days=1),
        as_of=NOW + timedelta(minutes=30),
    )
    assert await repository.list_bars(query) == [base]
    assert await repository.list_bars(
        query.model_copy(update={"as_of": NOW + timedelta(hours=2)})
    ) == [restored]
    async with database.session() as session:
        stored_base = await session.scalar(
            select(MarketBarRow).where(MarketBarRow.bar_id == base.bar_id)
        )
        revisions = (
            await session.scalars(
                select(MarketBarRevisionRow).where(MarketBarRevisionRow.bar_id == base.bar_id)
            )
        ).all()
    assert stored_base is not None
    assert stored_base.available_at == NOW
    assert stored_base.payload["close"] == base.model_dump(mode="json")["close"]
    assert len(revisions) == 2
    assert {revision.source_checksum_sha256 for revision in revisions} == {"a" * 64, "b" * 64}


async def test_sto_027_macro_release_replay_is_idempotent_and_ties_are_deterministic(
    database: Database,
) -> None:
    token = uuid4().hex
    series = MacroSeries(
        series_id=f"macro:CN:REPLAY:{token}",
        region=Region.CN,
        authority="Storage replay authority",
        code=f"REPLAY-{token}",
        name="Storage replay series",
        frequency=Frequency.MONTHLY,
        unit="index",
        transformation="level",
        seasonal_adjustment="not_adjusted",
        source=_source(f"series-replay-{token}"),
    )
    base = MacroRelease(
        release_id=f"release-replay-{token}",
        series_id=series.series_id,
        region=Region.CN,
        release_name="Storage replay release",
        scheduled_at=NOW + timedelta(days=3),
        available_at=NOW,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        unit="index",
        status="scheduled",
        source=_source(f"release-replay-{token}"),
    )
    revision = base.model_copy(
        update={
            "released_at": NOW,
            "actual": Decimal("101"),
            "status": "released",
            "source": _source(f"release-replay-revision-{token}", checksum="b" * 64),
        }
    )

    async with UnitOfWork(database).transaction() as session:
        lease, _ = await IngestionRunRepository(session).acquire_run(
            _ingest_request().model_copy(update={"dataset": Dataset.MACRO_RELEASES}),
            idempotency_key=f"release-replay-run-{token}",
            run_id=uuid4(),
            now=NOW,
            lease_expires_at=NOW + timedelta(minutes=1),
        )
        assert lease is not None
        repository = NormalizedFactRepository(session, ingestion_run_id=lease.run_id)
        await repository.upsert_macro_series(series)
        await repository.upsert_macro_release(base)
        await repository.upsert_macro_release(base)
        await repository.upsert_macro_release(revision)

    async with database.session() as session:
        revision_count = await session.scalar(
            select(func.count())
            .select_from(MacroReleaseRevisionRow)
            .where(MacroReleaseRevisionRow.release_id == base.release_id)
        )
    assert revision_count == 1
    assert await PostgresDataRepository(database).list_macro_releases(
        MacroReleaseQuery(
            regions={Region.CN},
            scheduled_from=NOW + timedelta(days=2),
            scheduled_to=NOW + timedelta(days=4),
            as_of=NOW,
        )
    ) == [revision]


async def test_sto_028_date_only_release_and_news_are_persisted_and_queryable(
    database: Database,
) -> None:
    token = uuid4().hex
    series = MacroSeries(
        series_id=f"macro:CN:DATE_ONLY:{token}",
        region=Region.CN,
        authority="Storage date-only authority",
        code=f"DATE_ONLY-{token}",
        name="Storage date-only series",
        frequency=Frequency.MONTHLY,
        unit="index",
        transformation="level",
        seasonal_adjustment="not_adjusted",
        source=_source(f"date-only-series-{token}"),
    )
    release_date = date(2040, 1, 1)
    release = MacroRelease(
        release_id=f"date-only-release-{token}",
        series_id=series.series_id,
        region=Region.CN,
        release_name="Date-only storage release",
        scheduled_at=None,
        scheduled_date=release_date,
        time_precision="date",
        available_at=NOW,
        period_start=release_date,
        period_end=release_date,
        unit="index",
        status="scheduled",
        source=_source(f"date-only-release-{token}"),
    )
    event = news_event().model_copy(
        update={
            "news_id": f"date-only-news-{token}",
            "published_at": None,
            "published_date": NOW.date(),
            "time_precision": "date",
            "regions": [Region.HK],
            "topics": ["date-only-storage"],
            "source": _source(f"date-only-news-{token}"),
        }
    )

    async with UnitOfWork(database).transaction() as session:
        run_id = uuid4()
        session.add(
            ProviderRunRow(
                run_id=run_id,
                idempotency_key=f"date-only-run-{token}",
                provider_role="storage.contract.date_only",
                dataset=Dataset.MACRO_RELEASES.value,
                status="succeeded",
                started_at=NOW,
                finished_at=NOW,
                records_fetched=2,
                records_accepted=2,
                records_rejected=0,
                details={},
                request_payload={},
            )
        )
        await session.flush()
        repository = NormalizedFactRepository(session, ingestion_run_id=run_id)
        await repository.upsert_macro_series(series)
        await repository.upsert_macro_release(release)
        await repository.upsert_news_event(event)

    repository = PostgresDataRepository(database)
    assert await repository.list_macro_releases(
        MacroReleaseQuery(
            regions={Region.CN},
            scheduled_from=datetime(2039, 12, 31, tzinfo=NOW.tzinfo),
            scheduled_to=datetime(2040, 1, 2, tzinfo=NOW.tzinfo),
            as_of=NOW,
        )
    ) == [release]
    assert await repository.list_news(
        NewsQuery(
            regions={Region.HK},
            published_from=NOW,
            published_to=NOW + timedelta(hours=1),
            as_of=NOW,
            topics={"date-only-storage"},
        )
    ) == [event]
