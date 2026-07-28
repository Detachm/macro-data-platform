from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from docker.errors import DockerException
from sqlalchemy import func, select
from testcontainers.postgres import PostgresContainer

from macro_platform.contracts.common import AssetClass, AvailabilityBasis, Region, SourceRef
from macro_platform.contracts.market import (
    Adjustment,
    Instrument,
    InstrumentStatus,
    Interval,
    MarketBar,
)
from macro_platform.contracts.provider import Dataset, IngestJobRequest, IngestJobResult
from macro_platform.jobs.ingestion_checkpoint import CommittedPage, IngestionCheckpointService
from macro_platform.jobs.runner import IngestionExecutionContext, JobRunner
from macro_platform.jobs.scheduler import (
    CheckpointedScheduledTask,
    PostgresReportDateLock,
    PostgresScheduledTaskCheckpointStore,
    ScheduledIngestionWorker,
)
from macro_platform.normalization.common import canonical_json_checksum
from macro_platform.services.report_input_materializer import (
    PostgresReportInputEvidenceStore,
    PostgresReportInputSnapshotStore,
    ReportInputSnapshotMaterializer,
)
from macro_platform.storage.database import Database
from macro_platform.storage.models import MarketBarRow, ReportInputSnapshotRow
from macro_platform.storage.repositories import IngestionCheckpointRepository
from macro_platform.storage.unit_of_work import UnitOfWork

pytestmark = pytest.mark.integration


@pytest.fixture
def postgresql_url() -> Iterator[str]:
    supplied_url = os.environ.get("CONTRACT_TEST_DATABASE_URL")
    if supplied_url is not None:
        yield supplied_url
        return
    try:
        with PostgresContainer(
            "postgres:16-alpine", username="macro", password="macro", dbname="macro_data_test"
        ) as postgres:
            yield postgres.get_connection_url().replace("psycopg2", "asyncpg")
    except DockerException as error:
        pytest.skip(f"Docker is unavailable for scheduled worker PostgreSQL contracts: {error}")


@pytest.fixture
def migrated_postgresql_url(postgresql_url: str) -> Iterator[str]:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["database_url"] = postgresql_url
    command.upgrade(config, "head")
    yield postgresql_url


@pytest.fixture
async def database(migrated_postgresql_url: str) -> AsyncIterator[Database]:
    value = Database(migrated_postgresql_url)
    try:
        assert await value.ready()
        yield value
    finally:
        await value.dispose()


async def test_job_029_postgres_advisory_lock_excludes_the_second_worker(
    postgresql_url: str,
) -> None:
    first_database = Database(postgresql_url)
    second_database = Database(postgresql_url)
    report_date = date(2026, 7, 27)
    try:
        first_lock = PostgresReportDateLock(first_database)
        second_lock = PostgresReportDateLock(second_database)
        async with (
            first_lock.hold(report_date) as first_acquired,
            second_lock.hold(report_date) as second_acquired,
        ):
            assert first_acquired is True
            assert second_acquired is False
        async with second_lock.hold(report_date) as acquired_after_release:
            assert acquired_after_release is True
    finally:
        await first_database.dispose()
        await second_database.dispose()


NOW = datetime(2026, 7, 28, 0, 0, tzinfo=UTC)
REPORT_DATE = date(2026, 7, 28)
_CN_INSTRUMENTS = (
    ("ins_cn_index_sse_composite", "XSHG:000001", "000001"),
    ("ins_cn_index_csi300", "XSHG:000300", "000300"),
    ("ins_cn_index_szse_component", "XSHE:399001", "399001"),
)


class _ThreePageHandler:
    """Checkpointed handler used only to prove worker persistence behavior."""

    provider_id = "job-029.integration.provider"

    def __init__(self) -> None:
        self.cursors: list[str | None] = []

    async def run(self, request: IngestJobRequest) -> IngestJobResult:
        raise AssertionError("scheduler integration must use JobRunner checkpointed execution")

    async def run_checkpointed(
        self,
        request: IngestJobRequest,
        checkpoints: IngestionCheckpointService,
        database: Database,
        execution: IngestionExecutionContext,
    ) -> IngestJobResult:
        self.cursors.append(request.cursor)
        page_no = 0 if request.cursor is None else int(request.cursor.removeprefix("page-")) - 1
        instrument_id, canonical_symbol, local_symbol = _CN_INSTRUMENTS[page_no]
        bar = _bar(
            instrument_id=instrument_id,
            canonical_symbol=canonical_symbol,
            local_symbol=local_symbol,
            report_date=request.end.date(),
        )
        next_cursor = None if page_no == len(_CN_INSTRUMENTS) - 1 else f"page-{page_no + 2}"
        page = CommittedPage(
            provider_role=request.provider_role,
            dataset=request.dataset,
            region=Region.CN.value,
            page_fingerprint=canonical_json_checksum(
                {"request": request.model_dump(mode="json"), "bar_id": bar.bar_id}
            ),
            source_watermark=f"report-{request.end.date().isoformat()}",
            next_cursor=next_cursor,
            accepted_record_ids=[bar.source.provider_record_id],
        )
        async with UnitOfWork(database).transaction() as session:
            repository = IngestionCheckpointRepository(session, ingestion_run_id=execution.run_id)

            async def write_records(_: object) -> None:
                await repository.upsert_instrument(
                    _instrument(
                        instrument_id=instrument_id,
                        canonical_symbol=canonical_symbol,
                        local_symbol=local_symbol,
                    )
                )
                await repository.upsert_bar(bar)

            committed = await checkpoints.commit_page(repository, page, write_records)
        return IngestJobResult(
            run_id=execution.run_id,
            status="succeeded",
            provider_role=request.provider_role,
            dataset=request.dataset,
            started_at=NOW,
            finished_at=NOW,
            records_fetched=1,
            records_accepted=1,
            records_rejected=0,
            records_inserted=1 if committed else 0,
            records_updated=0,
            next_cursor=next_cursor,
            source_watermark=page.source_watermark,
        )


def _source(local_symbol: str) -> SourceRef:
    return SourceRef(
        provider_id=_ThreePageHandler.provider_id,
        provider_record_id=f"{local_symbol}:{REPORT_DATE.isoformat()}",
        source_name="Job 029 PostgreSQL contract provider",
        source_url=f"https://example.com/{local_symbol}",
        source_symbol=local_symbol,
        retrieved_at=NOW,
        checksum_sha256=canonical_json_checksum({"symbol": local_symbol, "date": REPORT_DATE}),
    )


def _instrument(*, instrument_id: str, canonical_symbol: str, local_symbol: str) -> Instrument:
    return Instrument(
        instrument_id=instrument_id,
        canonical_symbol=canonical_symbol,
        region=Region.CN,
        venue_mic=canonical_symbol.partition(":")[0],
        local_symbol=local_symbol,
        name=f"Integration {local_symbol}",
        asset_class=AssetClass.INDEX,
        currency="CNY",
        timezone="Asia/Shanghai",
        status=InstrumentStatus.ACTIVE,
        valid_from=date(1990, 1, 1),
        source=_source(local_symbol),
    )


def _bar(
    *, instrument_id: str, canonical_symbol: str, local_symbol: str, report_date: date
) -> MarketBar:
    trading_date = report_date - timedelta(days=1)
    return MarketBar(
        bar_id=f"bar-{instrument_id}-{trading_date.isoformat()}",
        instrument_id=instrument_id,
        canonical_symbol=canonical_symbol,
        region=Region.CN,
        interval=Interval.D1,
        bar_start=datetime.combine(trading_date, datetime.min.time(), tzinfo=UTC),
        bar_end=datetime.combine(report_date, datetime.min.time(), tzinfo=UTC),
        trading_date=trading_date,
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10.5"),
        volume=Decimal("100"),
        currency="CNY",
        adjustment=Adjustment.RAW,
        available_at=NOW,
        availability_basis=AvailabilityBasis.FIRST_SEEN,
        source=_source(local_symbol),
    )


def _request(report_date: date, as_of: datetime, cursor: str | None) -> IngestJobRequest:
    return IngestJobRequest(
        provider_role="cn.bars.primary",
        dataset=Dataset.BARS,
        regions={Region.CN},
        start=datetime.combine(report_date - timedelta(days=14), datetime.min.time(), tzinfo=UTC),
        end=datetime.combine(report_date, datetime.min.time(), tzinfo=UTC),
        as_of=as_of,
        cursor=cursor,
    )


@pytest.mark.e2e
async def test_job_029_postgres_worker_resumes_pages_materializes_quality_and_backfills(
    database: Database,
) -> None:
    handler = _ThreePageHandler()
    runner = JobRunner(handler, database=database, now=lambda: NOW)
    checkpoint_store = PostgresScheduledTaskCheckpointStore(database)
    first_checkpoint = await checkpoint_store.begin_or_load(
        report_date=REPORT_DATE,
        task_id="cn.daily-bars",
        provider_role="cn.bars.primary",
        dataset=Dataset.BARS,
        region=Region.CN.value,
        request_as_of=NOW + timedelta(days=1),
    )
    first_result = await runner.execute(
        _request(REPORT_DATE, first_checkpoint.request_as_of, first_checkpoint.next_cursor)
    )
    await checkpoint_store.advance(
        first_checkpoint,
        run_id=first_result.run_id,
        next_cursor=first_result.next_cursor,
        source_watermark=first_result.source_watermark,
        records_accepted=first_result.records_accepted,
        records_rejected=first_result.records_rejected,
    )
    task = CheckpointedScheduledTask(
        task_id="cn.daily-bars",
        required=True,
        provider_role="cn.bars.primary",
        dataset=Dataset.BARS,
        region=Region.CN,
        executor=runner,
        checkpoint_store=checkpoint_store,
        request_factory=_request,
        now=lambda: NOW,
    )
    materializer = ReportInputSnapshotMaterializer(
        evidence_store=PostgresReportInputEvidenceStore(database),
        snapshot_store=PostgresReportInputSnapshotStore(database),
        now=lambda: NOW,
        cutoff_at=lambda _: NOW,
    )
    worker = ScheduledIngestionWorker(
        tasks=[task],
        report_date_lock=PostgresReportDateLock(database),
        input_materializer=materializer,
    )

    recovered = await worker.run_for_date(REPORT_DATE)
    backfill = await worker.backfill(
        REPORT_DATE + timedelta(days=1), REPORT_DATE + timedelta(days=2)
    )

    assert handler.cursors == [
        None,
        "page-2",
        "page-3",
        None,
        "page-2",
        "page-3",
        None,
        "page-2",
        "page-3",
    ]
    assert recovered.task_results[0].record_count == 3
    assert recovered.snapshot_id is not None
    assert recovered.status == "blocked"
    assert [result.snapshot_id is not None for result in backfill] == [True, True]
    snapshot_ids = [
        recovered.snapshot_id,
        *(result.snapshot_id for result in backfill),
    ]
    assert all(snapshot_id is not None for snapshot_id in snapshot_ids)
    async with database.session() as session:
        bar_count = await session.scalar(
            select(func.count())
            .select_from(MarketBarRow)
            .where(MarketBarRow.provider_id == _ThreePageHandler.provider_id)
        )
        snapshot_dates = (
            await session.scalars(
                select(ReportInputSnapshotRow.report_date)
                .where(ReportInputSnapshotRow.snapshot_id.in_(snapshot_ids))
                .order_by(ReportInputSnapshotRow.report_date)
            )
        ).all()
    assert bar_count == 9
    assert snapshot_dates == [
        REPORT_DATE,
        REPORT_DATE + timedelta(days=1),
        REPORT_DATE + timedelta(days=2),
    ]
