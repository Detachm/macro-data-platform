from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from docker.errors import DockerException
from sqlalchemy import func, select
from testcontainers.postgres import PostgresContainer

from macro_platform.contracts.common import AssetClass, AvailabilityBasis, Region, SourceRef
from macro_platform.contracts.macro import MacroRelease
from macro_platform.contracts.market import (
    Adjustment,
    Instrument,
    InstrumentStatus,
    Interval,
    MarketBar,
)
from macro_platform.contracts.provider import (
    Dataset,
    FetchContext,
    IngestJobRequest,
    IngestJobResult,
    ProviderCapabilities,
    ProviderPage,
)
from macro_platform.jobs.ingestion_checkpoint import CommittedPage, IngestionCheckpointService
from macro_platform.jobs.news_macro_ingestion import MacroReleaseIngestHandler, NewsIngestHandler
from macro_platform.jobs.runner import IngestionExecutionContext, JobRunner
from macro_platform.jobs.scheduler import (
    CheckpointedScheduledTask,
    PostgresReportDateLock,
    PostgresScheduledTaskCheckpointStore,
    ScheduledIngestionWorker,
    ScheduledTaskResult,
)
from macro_platform.normalization.common import canonical_json_checksum
from macro_platform.services.report_input_materializer import (
    PostgresReportInputEvidenceStore,
    PostgresReportInputSnapshotStore,
    ReportInputSnapshotMaterializer,
)
from macro_platform.storage.database import Database
from macro_platform.storage.models import (
    MacroReleaseRow,
    MacroSeriesRow,
    MarketBarRow,
    NewsEventRow,
    ReportInputSnapshotRow,
)
from macro_platform.storage.repositories import IngestionCheckpointRepository
from macro_platform.storage.unit_of_work import UnitOfWork
from tests.helpers import news_event

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


async def test_job_029_reclaimed_checkpoint_fences_a_stale_worker(database: Database) -> None:
    checkpoint_store = PostgresScheduledTaskCheckpointStore(database)
    report_date = date(2026, 8, 3)
    original = await checkpoint_store.begin_or_load(
        report_date=report_date,
        task_id="cn.daily-bars.fence",
        provider_role="cn.bars.primary",
        dataset=Dataset.BARS,
        region=Region.CN.value,
        request_as_of=NOW + timedelta(days=1),
        lease_owner_id=uuid4(),
    )
    runner = JobRunner(_ThreePageHandler(), database=database, now=lambda: NOW)
    result = await runner.execute(_request(report_date, original.request_as_of, None))
    reclaimed = await checkpoint_store.begin_or_load(
        report_date=report_date,
        task_id="cn.daily-bars.fence",
        provider_role="cn.bars.primary",
        dataset=Dataset.BARS,
        region=Region.CN.value,
        request_as_of=NOW + timedelta(days=1),
        lease_owner_id=uuid4(),
    )

    assert reclaimed.lease_epoch == original.lease_epoch + 1
    assert reclaimed.lease_owner_id != original.lease_owner_id
    with pytest.raises(RuntimeError, match="lost before it advanced"):
        await checkpoint_store.advance(
            original,
            run_id=result.run_id,
            next_cursor=result.next_cursor,
            source_watermark=result.source_watermark,
            records_accepted=result.records_accepted,
            records_rejected=result.records_rejected,
        )

    advanced = await checkpoint_store.advance(
        reclaimed,
        run_id=result.run_id,
        next_cursor=result.next_cursor,
        source_watermark=result.source_watermark,
        records_accepted=result.records_accepted,
        records_rejected=result.records_rejected,
    )
    assert advanced.status == "active"
    assert advanced.lease_epoch == reclaimed.lease_epoch


async def test_job_029_checkpointed_calendar_and_headline_tasks_persist_facts(
    database: Database,
) -> None:
    release_source = SourceRef(
        provider_id="job-029.release-provider",
        provider_record_id="calendar-row-1",
        source_name="Test release calendar",
        source_url="https://example.com/calendar/1",
        retrieved_at=NOW,
        checksum_sha256="c" * 64,
    )
    release = MacroRelease(
        release_id="job-029-release-1",
        series_id="macro:CN:TEST:release_calendar",
        region=Region.CN,
        release_name="Test calendar release",
        scheduled_at=NOW + timedelta(days=1),
        available_at=NOW,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        unit="index",
        status="scheduled",
        source=release_source,
    )
    news = news_event().model_copy(
        update={
            "news_id": "job-029-news-1",
            "regions": [Region.HK],
            "published_at": NOW - timedelta(hours=1),
            "published_date": None,
            "available_at": NOW,
            "source": SourceRef(
                provider_id="job-029.news-provider",
                provider_record_id="headline-row-1",
                source_name="Test official headlines",
                source_url="https://example.com/headline/1",
                retrieved_at=NOW,
                checksum_sha256="d" * 64,
            ),
        }
    )
    unapproved_us_release = release.model_copy(
        update={
            "release_id": "job-029-unapproved-us-release-1",
            "series_id": "macro:US:TEST:release_calendar",
            "region": Region.US,
            "release_name": "Unapproved US fixture calendar",
            "source": SourceRef(
                provider_id="job-029.unapproved-us-macro-fixture",
                provider_record_id="unapproved-us-calendar-row-1",
                source_name="Unapproved US fixture calendar",
                source_url="https://example.com/unapproved-us-calendar/1",
                retrieved_at=NOW,
                checksum_sha256="e" * 64,
            ),
        }
    )

    class _ReleaseProvider:
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(
                provider_id=release_source.provider_id,
                regions={Region.CN},
                datasets={Dataset.MACRO_RELEASES},
                max_page_size=1000,
                supports_point_in_time=False,
                supports_revisions=False,
                supports_full_text=False,
                external_llm_allowed=True,
            )

        async def fetch_macro_releases(
            self, _query: object, _context: FetchContext
        ) -> ProviderPage[MacroRelease]:
            return ProviderPage(items=[release], fetched_at=NOW, complete=True)

    class _NewsProvider:
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(
                provider_id=news.source.provider_id,
                regions={Region.HK},
                datasets={Dataset.NEWS},
                max_page_size=1000,
                supports_point_in_time=False,
                supports_revisions=False,
                supports_full_text=False,
                external_llm_allowed=True,
            )

        async def fetch_news(self, _query: object, _context: FetchContext) -> ProviderPage[object]:
            return ProviderPage(items=[news], fetched_at=NOW, complete=True)

    class _UnapprovedUsFixtureProvider:
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(
                provider_id=unapproved_us_release.source.provider_id,
                regions={Region.US},
                datasets={Dataset.MACRO_RELEASES},
                max_page_size=1000,
                supports_point_in_time=False,
                supports_revisions=False,
                supports_full_text=False,
                external_llm_allowed=True,
            )

        async def fetch_macro_releases(
            self, _query: object, _context: FetchContext
        ) -> ProviderPage[MacroRelease]:
            return ProviderPage(items=[unapproved_us_release], fetched_at=NOW, complete=True)

    request_as_of = NOW + timedelta(days=1)
    release_result = await JobRunner(
        MacroReleaseIngestHandler(
            _ReleaseProvider(),
            provider_role="cn.macro.primary",
            region=Region.CN,
            timeout_seconds=30,
            now=lambda: NOW,
        ),
        database=database,
        now=lambda: NOW,
    ).execute(
        IngestJobRequest(
            provider_role="cn.macro.primary",
            dataset=Dataset.MACRO_RELEASES,
            regions={Region.CN},
            start=NOW,
            end=NOW + timedelta(days=8),
            as_of=request_as_of,
        )
    )
    news_result = await JobRunner(
        NewsIngestHandler(
            _NewsProvider(),  # type: ignore[arg-type]
            provider_role="hk.news.primary",
            region=Region.HK,
            timeout_seconds=30,
            now=lambda: NOW,
        ),
        database=database,
        now=lambda: NOW,
    ).execute(
        IngestJobRequest(
            provider_role="hk.news.primary",
            dataset=Dataset.NEWS,
            regions={Region.HK},
            start=NOW - timedelta(days=1),
            end=NOW + timedelta(days=1),
            as_of=request_as_of,
        )
    )
    replayed_news_result = await JobRunner(
        NewsIngestHandler(
            _NewsProvider(),  # type: ignore[arg-type]
            provider_role="hk.news.primary",
            region=Region.HK,
            timeout_seconds=30,
            now=lambda: NOW,
        ),
        database=database,
        now=lambda: NOW,
    ).execute(
        IngestJobRequest(
            provider_role="hk.news.primary",
            dataset=Dataset.NEWS,
            regions={Region.HK},
            start=NOW - timedelta(days=1),
            end=NOW + timedelta(days=1),
            as_of=request_as_of + timedelta(minutes=1),
        )
    )
    unapproved_us_result = await JobRunner(
        MacroReleaseIngestHandler(
            _UnapprovedUsFixtureProvider(),
            provider_role="test.us.macro.fixture",
            region=Region.US,
            timeout_seconds=30,
            now=lambda: NOW,
        ),
        database=database,
        now=lambda: NOW,
    ).execute(
        IngestJobRequest(
            provider_role="test.us.macro.fixture",
            dataset=Dataset.MACRO_RELEASES,
            regions={Region.US},
            start=NOW,
            end=NOW + timedelta(days=8),
            as_of=request_as_of,
        )
    )

    assert (
        release_result.records_inserted,
        news_result.records_inserted,
        unapproved_us_result.records_inserted,
    ) == (1, 1, 1)
    assert replayed_news_result.run_id != news_result.run_id
    assert replayed_news_result.records_accepted == 1
    async with database.session() as session:
        stored_release = await session.get(MacroReleaseRow, release.release_id)
        stored_series = await session.get(MacroSeriesRow, release.series_id)
        stored_news = await session.get(NewsEventRow, news.news_id)
    assert stored_release is not None
    assert stored_series is not None
    assert stored_news is not None
    assert stored_news.ingestion_run_id == news_result.run_id
    evidence = await PostgresReportInputEvidenceStore(database).collect(
        report_date=NOW.date(),
        as_of=NOW,
        cutoff_at=NOW,
        task_results=(
            ScheduledTaskResult(
                task_id="cn.macro-release-calendar",
                provider_role="cn.macro.primary",
                dataset=Dataset.MACRO_RELEASES,
                region=Region.CN,
                status="succeeded",
                run_id=release_result.run_id,
            ),
            ScheduledTaskResult(
                task_id="hk.official-headlines",
                provider_role="hk.news.primary",
                dataset=Dataset.NEWS,
                region=Region.HK,
                status="succeeded",
                run_id=replayed_news_result.run_id,
            ),
        ),
    )
    by_input_id = {item.input_id: item for item in evidence}
    assert by_input_id["calendar.macro_releases_7d"].status == "missing"
    hk_market = by_input_id["market.hk.core_indices.previous_close"]
    assert hk_market.status == "missing"
    assert "HK equities" in hk_market.reason
    assert by_input_id["news.cn.official_headlines_24h"].status == "missing"
    hk_news = by_input_id["news.hk.official_headlines_24h"]
    assert hk_news.status == "available"
    assert hk_news.facts == (
        {
            "fact_id": hk_news.facts[0]["fact_id"],
            "input_id": "news.hk.official_headlines_24h",
            "fact_type": "news_event",
            "section_id": "hk_highlights",
            "news_id": news.news_id,
            "title": news.title,
            "summary": news.summary,
            "published_at": news.published_at.isoformat().replace("+00:00", "Z"),
            "published_date": None,
            "content_mode": news.content_mode.value,
            "label": news.title,
            "display_text": f"{news.title}: {news.summary}",
            "value": None,
            "available_at": news.available_at.isoformat().replace("+00:00", "Z"),
            "report_date": NOW.date().isoformat(),
            "source_ref_ids": [hk_news.source_references[0]["source_ref_id"]],
        },
    )


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


class _MarketBarsPageHandler:
    """One committed page used to prove revision payload selection."""

    provider_id = "job-029.revision-provider"

    def __init__(self, bars: tuple[MarketBar, ...]) -> None:
        self._bars = bars

    async def run(self, request: IngestJobRequest) -> IngestJobResult:
        raise AssertionError("revision evidence must use JobRunner checkpointed execution")

    async def run_checkpointed(
        self,
        request: IngestJobRequest,
        checkpoints: IngestionCheckpointService,
        database: Database,
        execution: IngestionExecutionContext,
    ) -> IngestJobResult:
        page = CommittedPage(
            provider_role=request.provider_role,
            dataset=request.dataset,
            region=Region.CN.value,
            page_fingerprint=canonical_json_checksum(
                {
                    "request": request.model_dump(mode="json"),
                    "checksums": [bar.source.checksum_sha256 for bar in self._bars],
                }
            ),
            source_watermark="revision-evidence",
            next_cursor=None,
            accepted_record_ids=[bar.source.provider_record_id for bar in self._bars],
        )
        async with UnitOfWork(database).transaction() as session:
            repository = IngestionCheckpointRepository(session, ingestion_run_id=execution.run_id)

            async def write_records(_: object) -> None:
                for bar in self._bars:
                    await repository.upsert_instrument(
                        _instrument(
                            instrument_id=bar.instrument_id,
                            canonical_symbol=bar.canonical_symbol,
                            local_symbol=bar.source.source_symbol or bar.instrument_id,
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
            records_fetched=len(self._bars),
            records_accepted=len(self._bars),
            records_rejected=0,
            records_inserted=len(self._bars) if committed else 0,
            records_updated=0,
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


def _revision_source(*, local_symbol: str, checksum_seed: str) -> SourceRef:
    return SourceRef(
        provider_id=_MarketBarsPageHandler.provider_id,
        provider_record_id=f"{local_symbol}:{REPORT_DATE.isoformat()}",
        source_name="Job 029 revision evidence provider",
        source_url=f"https://example.com/revision/{local_symbol}",
        source_symbol=local_symbol,
        retrieved_at=NOW,
        checksum_sha256=canonical_json_checksum(
            {"symbol": local_symbol, "revision": checksum_seed}
        ),
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


def _revision_bar(
    *,
    instrument_id: str,
    canonical_symbol: str,
    local_symbol: str,
    report_date: date,
    close: Decimal,
    available_at: datetime,
    checksum_seed: str,
) -> MarketBar:
    return _bar(
        instrument_id=instrument_id,
        canonical_symbol=canonical_symbol,
        local_symbol=local_symbol,
        report_date=report_date,
    ).model_copy(
        update={
            "close": close,
            "high": Decimal("12"),
            "available_at": available_at,
            "source": _revision_source(local_symbol=local_symbol, checksum_seed=checksum_seed),
        }
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
        lease_owner_id=uuid4(),
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
            .where(
                MarketBarRow.provider_id == _ThreePageHandler.provider_id,
                MarketBarRow.trading_date.in_(
                    [
                        REPORT_DATE - timedelta(days=1),
                        REPORT_DATE,
                        REPORT_DATE + timedelta(days=1),
                    ]
                ),
            )
        )
        snapshot_dates = (
            await session.scalars(
                select(ReportInputSnapshotRow.report_date)
                .where(ReportInputSnapshotRow.snapshot_id.in_(snapshot_ids))
                .order_by(ReportInputSnapshotRow.report_date)
            )
        ).all()
        recovered_snapshot = await session.get(
            ReportInputSnapshotRow,
            recovered.snapshot_id,
        )
    assert bar_count == 9
    assert snapshot_dates == [
        REPORT_DATE,
        REPORT_DATE + timedelta(days=1),
        REPORT_DATE + timedelta(days=2),
    ]
    assert recovered_snapshot is not None
    market_facts = [
        fact
        for fact in recovered_snapshot.payload["facts"]
        if fact.get("input_id") == "market.cn.core_indices.previous_close"
    ]
    closes_by_instrument = {fact["instrument_id"]: fact["close"] for fact in market_facts}
    assert closes_by_instrument == {
        "ins_cn_index_csi300": "10.5",
        "ins_cn_index_sse_composite": "10.5",
        "ins_cn_index_szse_component": "10.5",
    }
    assert all(fact["trading_date"] == "2026-07-27" for fact in market_facts)
    assert all(len(fact["source_ref_ids"]) == 1 for fact in market_facts)


async def test_job_029_evidence_uses_committed_task_runs_and_excludes_late_market_bars(
    database: Database,
) -> None:
    runner = JobRunner(_ThreePageHandler(), database=database, now=lambda: NOW)
    task = CheckpointedScheduledTask(
        task_id="cn.daily-bars",
        required=True,
        provider_role="cn.bars.primary",
        dataset=Dataset.BARS,
        region=Region.CN,
        executor=runner,
        checkpoint_store=PostgresScheduledTaskCheckpointStore(database),
        request_factory=_request,
        now=lambda: NOW,
    )
    task_result = await task.run(REPORT_DATE)
    evidence_store = PostgresReportInputEvidenceStore(database)

    late = await evidence_store.collect(
        report_date=REPORT_DATE,
        as_of=NOW,
        cutoff_at=NOW - timedelta(minutes=1),
        task_results=(task_result,),
    )
    unbound = await evidence_store.collect(
        report_date=REPORT_DATE,
        as_of=NOW,
        cutoff_at=NOW,
        task_results=(
            ScheduledTaskResult(
                task_id="cn.daily-bars",
                provider_role="cn.bars.primary",
                dataset=Dataset.BARS,
                region=Region.CN,
                status="succeeded",
                run_id=uuid4(),
            ),
        ),
    )

    late_market = {item.input_id: item for item in late}["market.cn.core_indices.previous_close"]
    unbound_market = {item.input_id: item for item in unbound}[
        "market.cn.core_indices.previous_close"
    ]
    assert (late_market.status, late_market.facts, late_market.source_references) == (
        "late",
        (),
        (),
    )
    assert unbound_market.status == "unavailable"
    assert unbound_market.facts == ()


async def test_job_029_market_evidence_uses_the_latest_revision_payload_before_cutoff(
    database: Database,
) -> None:
    revision_report_date = date(2026, 8, 4)
    base_bars = tuple(
        _revision_bar(
            instrument_id=instrument_id,
            canonical_symbol=canonical_symbol,
            local_symbol=local_symbol,
            report_date=revision_report_date,
            close=Decimal("10.5"),
            available_at=NOW - timedelta(minutes=2),
            checksum_seed="base",
        )
        for instrument_id, canonical_symbol, local_symbol in _CN_INSTRUMENTS
    )
    revised_bars = tuple(
        bar.model_copy(
            update={
                "close": Decimal("11.5"),
                "high": Decimal("12"),
                "available_at": NOW - timedelta(minutes=1),
                "source": _revision_source(
                    local_symbol=bar.source.source_symbol or bar.instrument_id,
                    checksum_seed="revision",
                ),
            }
        )
        for bar in base_bars
    )
    runner = JobRunner(
        _MarketBarsPageHandler(base_bars),
        database=database,
        now=lambda: NOW,
    )
    first = await runner.execute(_request(revision_report_date, NOW + timedelta(days=1), None))
    second = await JobRunner(
        _MarketBarsPageHandler(revised_bars),
        database=database,
        now=lambda: NOW,
    ).execute(_request(revision_report_date, NOW + timedelta(days=1), "revision"))
    replayed = await JobRunner(
        _MarketBarsPageHandler(revised_bars),
        database=database,
        now=lambda: NOW,
    ).execute(_request(revision_report_date, NOW + timedelta(days=1), "replayed-revision"))

    evidence = await PostgresReportInputEvidenceStore(database).collect(
        report_date=revision_report_date,
        as_of=NOW,
        cutoff_at=NOW,
        task_results=(
            ScheduledTaskResult(
                task_id="cn.daily-bars",
                provider_role="cn.bars.primary",
                dataset=Dataset.BARS,
                region=Region.CN,
                status="succeeded",
                run_id=second.run_id,
                run_ids=(first.run_id, second.run_id),
            ),
        ),
    )
    base_only_evidence = await PostgresReportInputEvidenceStore(database).collect(
        report_date=revision_report_date,
        as_of=NOW,
        cutoff_at=NOW,
        task_results=(
            ScheduledTaskResult(
                task_id="cn.daily-bars",
                provider_role="cn.bars.primary",
                dataset=Dataset.BARS,
                region=Region.CN,
                status="succeeded",
                run_id=first.run_id,
            ),
        ),
    )
    replayed_evidence = await PostgresReportInputEvidenceStore(database).collect(
        report_date=revision_report_date,
        as_of=NOW,
        cutoff_at=NOW,
        task_results=(
            ScheduledTaskResult(
                task_id="cn.daily-bars",
                provider_role="cn.bars.primary",
                dataset=Dataset.BARS,
                region=Region.CN,
                status="succeeded",
                run_id=replayed.run_id,
            ),
        ),
    )

    market = {item.input_id: item for item in evidence}["market.cn.core_indices.previous_close"]
    base_only_market = {item.input_id: item for item in base_only_evidence}[
        "market.cn.core_indices.previous_close"
    ]
    replayed_market = {item.input_id: item for item in replayed_evidence}[
        "market.cn.core_indices.previous_close"
    ]
    assert market.status == "revised"
    assert {fact["close"] for fact in market.facts} == {"11.5"}
    assert {fact["value"] for fact in market.facts} == {"11.5"}
    assert base_only_market.status == "available"
    assert {fact["close"] for fact in base_only_market.facts} == {"10.5"}
    assert replayed_market.status == "revised"
    assert {fact["close"] for fact in replayed_market.facts} == {"11.5"}
