from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from datetime import date, timedelta
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
from macro_platform.governance.source_policy import (
    ApprovalStatus,
    IngestionRetentionPolicy,
    ProductionSourcePolicy,
    RetentionRule,
    SourcePolicyEntry,
    SourcePolicyManifest,
)
from macro_platform.jobs.ingestion_checkpoint import CommittedPage, IngestionCheckpointService
from macro_platform.jobs.runner import JobRunner
from macro_platform.storage.database import Database
from macro_platform.storage.models import (
    DailyReportRow,
    DailyReportSourceRefRow,
    DeliveryAttemptRow,
    IngestPageCommitRow,
    MarketBarRow,
    MarketObservationRow,
    ProviderRunRow,
    ReportInputSnapshotRow,
)
from macro_platform.storage.reporting import DeliveryAttempt, ReportInputSnapshot, StoredDailyReport
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


async def _seed_0002_provider_run(database_url: str, run_id: UUID) -> None:
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
    finally:
        await database.dispose()


@pytest.fixture(scope="module")
def postgresql_url() -> Iterator[str]:
    """Exercise an upgrade from the prior released schema before repository tests."""

    supplied_url = os.environ.get("CONTRACT_TEST_DATABASE_URL")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    global _previous_schema_run_id
    _previous_schema_run_id = uuid4()
    if supplied_url is not None:
        config.attributes["database_url"] = supplied_url
        command.upgrade(config, "0002")
        asyncio.run(_seed_0002_provider_run(supplied_url, _previous_schema_run_id))
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
            asyncio.run(_seed_0002_provider_run(database_url, _previous_schema_run_id))
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
        self.retention_policy: IngestionRetentionPolicy | None = None
        self.run_id: UUID | None = None

    def set_retention_policy(self, policy: IngestionRetentionPolicy) -> None:
        self.retention_policy = policy

    def set_durable_run_id(self, run_id: UUID) -> None:
        self.run_id = run_id

    async def run(self, request: IngestJobRequest) -> IngestJobResult:
        raise AssertionError("checkpointed handler must use the durable runner path")

    async def run_checkpointed(
        self,
        request: IngestJobRequest,
        checkpoints: IngestionCheckpointService,
        database: Database,
    ) -> IngestJobResult:
        assert self.run_id is not None
        assert self.retention_policy is not None
        assert isinstance(checkpoints, IngestionCheckpointService)
        assert isinstance(database, Database)
        self.calls += 1
        return IngestJobResult(
            run_id=self.run_id,
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


def _production_policy() -> ProductionSourcePolicy:
    return ProductionSourcePolicy(
        SourcePolicyManifest(
            policy_version="storage-contract-test",
            entries=[
                SourcePolicyEntry(
                    policy_id="storage-contract-ingestion",
                    provider_id=_ProductionCheckpointedHandler.provider_id,
                    dataset=Dataset.MARKET_OBSERVATIONS,
                    regions={Region.CN},
                    owner="@kazming666",
                    credential_requirement="none",
                    ingestion_allowed=True,
                    external_llm_allowed=False,
                    citation_allowed=False,
                    retention_rule=RetentionRule.CANONICAL_FACTS,
                    approval_status=ApprovalStatus.APPROVED,
                    production_enabled=True,
                    evidence=["docs/adr/0002-daily-report-storage-idempotency.md"],
                )
            ],
        )
    )


async def test_rep_027_migration_upgrades_0002_to_current_schema(database: Database) -> None:
    async with database.session() as session:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        tables = set(
            (
                await session.scalars(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = 'public' AND tablename IN "
                        "('report_input_snapshots', 'daily_reports', 'daily_report_source_refs', "
                        "'delivery_attempts', 'macro_release_revisions')"
                    )
                )
            ).all()
        )
        preserved_run = await session.get(ProviderRunRow, _previous_schema_run_id)
    assert revision == "0003"
    assert tables == {
        "report_input_snapshots",
        "daily_reports",
        "daily_report_source_refs",
        "delivery_attempts",
        "macro_release_revisions",
    }
    assert preserved_run is not None
    assert preserved_run.idempotency_key is None
    assert preserved_run.request_payload == {}


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
            status="failed",
            response_payload={"error": "timeout after write"},
        )
        assert await repository.retry_delivery_attempt(delivery.delivery_id)

    async with UnitOfWork(database).transaction() as session:
        repository = ReportRepository(session)
        assert not await repository.put_input_snapshot(snapshot)
        assert not await repository.put_report(report)
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


async def test_rep_027_002_ingestion_run_and_report_recover_after_restart(
    database: Database, postgresql_url: str
) -> None:
    token = uuid4().hex
    request = _ingest_request()
    snapshot, report, delivery = _report_commands(token)

    async with UnitOfWork(database).transaction() as session:
        run_id, reserved = await IngestionRunRepository(session).reserve_run(
            request, idempotency_key=f"ingest-{token}"
        )
        assert reserved
        report_repository = ReportRepository(session)
        assert await report_repository.put_input_snapshot(snapshot)
        assert await report_repository.put_report(report)
        assert await report_repository.reserve_delivery_attempt(delivery)

    # Simulate a worker restart before it can acknowledge the running ingestion.
    async with UnitOfWork(database).transaction() as session:
        run_repository = IngestionRunRepository(session)
        replay_run_id, reserved = await run_repository.reserve_run(
            request, idempotency_key=f"ingest-{token}"
        )
        assert not reserved
        assert replay_run_id == run_id
        persisted = await run_repository.load_run(run_id)
        assert persisted is not None
        assert persisted.status == "running"
        await run_repository.complete_run(
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
            )
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


async def test_rep_027_production_runner_replays_completed_ingestion_without_provider_call(
    database: Database,
) -> None:
    handler = _ProductionCheckpointedHandler()
    runner = JobRunner(handler, database=database, source_policy=_production_policy())

    first = await runner.execute(_ingest_request())
    second = await runner.execute(_ingest_request())

    assert first == second
    assert handler.calls == 1


async def test_db_004_concurrent_page_replay_commits_one_fact_and_checkpoint(
    database: Database,
) -> None:
    token = uuid4().hex
    observation = MarketObservation(
        observation_id=f"concurrent-observation-{token}",
        region=Region.CN,
        scope_type=ScopeType.MARKET,
        scope_id="CN",
        metric_code="market.turnover",
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

    async def commit_once() -> bool:
        async with UnitOfWork(database).transaction() as session:
            repository = IngestionCheckpointRepository(session)

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
    assert await repository.list_instruments(InstrumentQuery(regions={Region.CN})) == [instrument]
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
