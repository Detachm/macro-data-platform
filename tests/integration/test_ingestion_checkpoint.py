from __future__ import annotations

import json
import os
from asyncio import Event, create_task, wait_for
from collections.abc import AsyncIterator, Iterator
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from docker.errors import DockerException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from testcontainers.postgres import PostgresContainer

from macro_platform.contracts.common import Region
from macro_platform.contracts.market import InstrumentQuery
from macro_platform.contracts.provider import Dataset, FetchContext, IngestJobRequest
from macro_platform.jobs.cn_hk_ingestion import CnHkFixtureIngestHandler
from macro_platform.jobs.ingestion_checkpoint import CommittedPage, IngestionCheckpointService
from macro_platform.jobs.runner import JobRunner
from macro_platform.providers.base import ProviderCursorError, UnsupportedCapabilityError
from macro_platform.providers.cn import CN_PROVIDER_ID, CnSyntheticProvider
from macro_platform.providers.hk import HK_PROVIDER_ID, HkSyntheticProvider
from macro_platform.storage.database import Database
from macro_platform.storage.models import (
    IngestAuditRow,
    JobWatermarkRow,
    MarketObservationRow,
    ProviderRunRow,
)
from macro_platform.storage.repositories import IngestionCheckpointRepository
from macro_platform.storage.unit_of_work import UnitOfWork

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def migrated_database_url() -> Iterator[str]:
    """Provision an empty database and exercise every migration to head."""
    supplied_url = os.environ.get("CONTRACT_TEST_DATABASE_URL")
    if supplied_url is not None:
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.attributes["database_url"] = supplied_url
        command.upgrade(config, "head")
        yield supplied_url
        return
    try:
        with PostgresContainer(
            "postgres:16-alpine", username="macro", password="macro", dbname="macro_data_test"
        ) as postgres:
            database_url = postgres.get_connection_url().replace("psycopg2", "asyncpg")
            config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
            config.attributes["database_url"] = database_url
            command.upgrade(config, "head")
            yield database_url
    except DockerException as error:
        pytest.skip(f"Docker is unavailable for PostgreSQL migration contracts: {error}")


@pytest.fixture
async def database(migrated_database_url: str) -> AsyncIterator[Database]:
    database = Database(migrated_database_url)
    if not await database.ready():
        await database.dispose()
        pytest.fail("testcontainers PostgreSQL did not become ready")
    try:
        yield database
    finally:
        await database.dispose()


def _page(
    region: Region,
    *,
    dataset: Dataset = Dataset.MARKET_OBSERVATIONS,
    fingerprint: str | None = None,
) -> CommittedPage:
    return CommittedPage(
        provider_role=f"{region.value.lower()}.macro.primary",
        dataset=dataset,
        region=region.value,
        page_fingerprint=fingerprint or uuid4().hex,
        source_watermark="2026-07-24T00:00:00Z",
        next_cursor="cursor-after-page",
        accepted_record_ids=[f"{region.value.lower()}-record-001"],
    )


def _provider_id(region: Region) -> str:
    return CN_PROVIDER_ID if region is Region.CN else HK_PROVIDER_ID


async def _create_durable_run(session: AsyncSession, *, region: Region, dataset: Dataset) -> UUID:
    run_id = uuid4()
    session.add(
        ProviderRunRow(
            run_id=run_id,
            idempotency_key=f"checkpoint-test-{run_id}",
            provider_role=f"{region.value.lower()}.checkpoint.test",
            dataset=dataset.value,
            status="running",
            started_at=datetime(2026, 7, 24, tzinfo=UTC),
            records_fetched=0,
            records_accepted=0,
            records_rejected=0,
            details={},
            request_payload={},
        )
    )
    await session.flush()
    return run_id


def _provider_for(region: Region) -> CnSyntheticProvider | HkSyntheticProvider:
    provider_cls = CnSyntheticProvider if region is Region.CN else HkSyntheticProvider
    return provider_cls.from_fixture("cursor_expired")


def _success_provider(region: Region) -> CnSyntheticProvider | HkSyntheticProvider:
    provider_cls = CnSyntheticProvider if region is Region.CN else HkSyntheticProvider
    return provider_cls.from_fixture("success")


def _ingest_request(region: Region) -> IngestJobRequest:
    return IngestJobRequest(
        provider_role=f"{region.value.lower()}.contract_fixture.market_observations",
        dataset=Dataset.MARKET_OBSERVATIONS,
        regions={region},
        start=datetime(2026, 7, 22, tzinfo=UTC),
        end=datetime(2026, 7, 23, tzinfo=UTC),
        as_of=datetime(2026, 7, 23, 8, tzinfo=UTC),
    )


async def test_db_001_empty_database_migrates_to_0005(database: Database) -> None:
    async with database.session() as session:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
    assert revision == "0005"


@pytest.mark.parametrize("region", [Region.CN, Region.HK])
async def test_cn_hk_runner_executes_durable_market_observation_ingestion(
    database: Database, region: Region
) -> None:
    request = _ingest_request(region)
    runner = JobRunner(
        CnHkFixtureIngestHandler(_success_provider(region)),
        database=database,
    )

    first = await runner.execute(request)
    second = await runner.execute(request)

    assert first.records_inserted == 1
    assert second == first
    async with database.session() as session:
        persisted = await session.scalar(
            select(func.count())
            .select_from(MarketObservationRow)
            .where(MarketObservationRow.region == region.value)
        )
        audits = await session.scalar(
            select(func.count())
            .select_from(IngestAuditRow)
            .where(IngestAuditRow.provider_id == _provider_id(region))
        )
        raw_audit = await session.scalar(
            select(IngestAuditRow).where(
                IngestAuditRow.provider_id == _provider_id(region),
                IngestAuditRow.audit_kind == "raw_timestamp_normalization",
            )
        )
    assert persisted == 1
    # A replay rehydrates the completed durable result instead of running the
    # handler again, so it must not append a second raw-timestamp audit.
    assert audits == 1
    assert raw_audit is not None
    assert raw_audit.payload["raw_timezone"] == (
        "Asia/Shanghai" if region is Region.CN else "Asia/Hong_Kong"
    )


@pytest.mark.parametrize("region", [Region.CN, Region.HK])
async def test_cn_hk_runner_persists_pit_rejection_before_outer_failure(
    database: Database, region: Region
) -> None:
    runner = JobRunner(
        CnHkFixtureIngestHandler(_success_provider(region), supports_point_in_time=False),
        database=database,
    )

    with pytest.raises(UnsupportedCapabilityError):
        await runner.execute(
            _ingest_request(region).model_copy(
                update={"provider_role": f"{region.value.lower()}.contract_fixture.pit_rejection"}
            )
        )

    async with database.session() as session:
        audit = await session.scalar(
            select(IngestAuditRow).where(
                IngestAuditRow.provider_id == _provider_id(region),
                IngestAuditRow.audit_kind == "unsupported_historical_pit",
            )
        )
    assert audit is not None


@pytest.mark.parametrize("region", [Region.CN, Region.HK])
async def test_cn_hk_handler_recovers_from_expired_cursor_with_watermark(
    database: Database, region: Region
) -> None:
    request = _ingest_request(region)
    await JobRunner(
        CnHkFixtureIngestHandler(_success_provider(region)),
        database=database,
    ).execute(request)
    expired_request = request.model_copy(update={"cursor": "expired-cursor"})
    handler = CnHkFixtureIngestHandler(_provider_for(region)).with_recovery_provider(
        _success_provider(region)
    )
    result = await JobRunner(
        handler,
        database=database,
    ).execute(expired_request)

    assert result.source_watermark is not None
    assert result.records_inserted == 0
    assert [warning.code for warning in result.warnings] == ["CURSOR_RECOVERED"]


@pytest.mark.parametrize("region", [Region.CN, Region.HK])
async def test_cn_hk_handler_rejects_recovery_from_a_different_source_snapshot(
    database: Database, region: Region, tmp_path: Path
) -> None:
    provider_role = f"{region.value.lower()}.contract_fixture.watermark_mismatch"
    request = _ingest_request(region).model_copy(update={"provider_role": provider_role})
    await JobRunner(
        CnHkFixtureIngestHandler(_success_provider(region)),
        database=database,
    ).execute(request)

    provider_cls = CnSyntheticProvider if region is Region.CN else HkSyntheticProvider
    fixture_payload = json.loads(
        (provider_cls.fixture_dir / "success.json").read_text(encoding="utf-8")
    )
    fixture_payload["source_watermark"] = f"{region.value.lower()}-new-source-snapshot"
    changed_snapshot = tmp_path / "changed-source-snapshot.json"
    changed_snapshot.write_text(json.dumps(fixture_payload), encoding="utf-8")

    async with database.session() as session:
        audits_before = await session.scalar(
            select(func.count())
            .select_from(IngestAuditRow)
            .where(IngestAuditRow.provider_id == _provider_id(region))
        )
        watermark_before = await session.get(
            JobWatermarkRow,
            (provider_role, Dataset.MARKET_OBSERVATIONS.value, region.value),
        )
        assert watermark_before is not None
        committed_watermark = watermark_before.watermark

    expired_request = request.model_copy(update={"cursor": "expired-cursor"})
    handler = CnHkFixtureIngestHandler(_provider_for(region)).with_recovery_provider(
        provider_cls(changed_snapshot)
    )
    with pytest.raises(ProviderCursorError) as error:
        await JobRunner(
            handler,
            database=database,
        ).execute(expired_request)

    assert error.value.code == "CURSOR_RECOVERY_MISMATCH"
    async with database.session() as session:
        audits_after = await session.scalar(
            select(func.count())
            .select_from(IngestAuditRow)
            .where(IngestAuditRow.provider_id == _provider_id(region))
        )
        watermark_after = await session.get(
            JobWatermarkRow,
            (provider_role, Dataset.MARKET_OBSERVATIONS.value, region.value),
        )
    assert audits_after == audits_before
    assert watermark_after is not None
    assert watermark_after.watermark == committed_watermark


@pytest.mark.parametrize("region", [Region.CN, Region.HK])
async def test_cn_hk_handler_persists_raw_timestamp_audits_across_market_pages(
    database: Database, region: Region, tmp_path: Path
) -> None:
    provider_cls = CnSyntheticProvider if region is Region.CN else HkSyntheticProvider
    fixture_payload = json.loads(
        (provider_cls.fixture_dir / "success.json").read_text(encoding="utf-8")
    )
    first_record = fixture_payload["pages"]["market_observations"]["items"][0]
    second_record = deepcopy(first_record)
    second_record.update(
        {
            "record_id": f"{first_record['record_id']}-second",
            "value": "866000000000",
            "period_start": "2026-07-22T02:30:00Z",
            "period_end": "2026-07-22T08:00:00Z",
            "observed_at": "2026-07-22T08:00:00Z",
            "raw_observed_at": "2026-07-22 16:00:00",
            "available_at": "2026-07-22T08:10:00Z",
            "source_url": f"{first_record['source_url']}?page=2",
            "provider_updated_at": "2026-07-22T08:10:00Z",
        }
    )
    fixture_payload["pages"]["market_observations"] = [
        {"cursor": None, "next_cursor": "market-next-1", "items": [first_record]},
        {"cursor": "market-next-1", "next_cursor": None, "items": [second_record]},
    ]
    paginated_fixture = tmp_path / "paginated-market-observations.json"
    paginated_fixture.write_text(json.dumps(fixture_payload), encoding="utf-8")

    provider_role = f"{region.value.lower()}.contract_fixture.paginated_market"
    request = _ingest_request(region).model_copy(update={"provider_role": provider_role})
    runner = JobRunner(
        CnHkFixtureIngestHandler(provider_cls(paginated_fixture)),
        database=database,
    )
    first = await runner.execute(request)
    assert first.next_cursor is not None
    second = await runner.execute(request.model_copy(update={"cursor": first.next_cursor}))

    assert first.records_inserted == 1
    assert second.records_inserted == 1
    assert second.next_cursor is None
    expected_record_ids = {first_record["record_id"], second_record["record_id"]}
    async with database.session() as session:
        persisted_record_ids = set(
            (
                await session.scalars(
                    select(MarketObservationRow.provider_record_id).where(
                        MarketObservationRow.provider_id == _provider_id(region),
                        MarketObservationRow.provider_record_id.in_(expected_record_ids),
                    )
                )
            ).all()
        )
        raw_audits = (
            await session.scalars(
                select(IngestAuditRow).where(
                    IngestAuditRow.run_id.in_([first.run_id, second.run_id]),
                    IngestAuditRow.audit_kind == "raw_timestamp_normalization",
                )
            )
        ).all()
    assert persisted_record_ids == expected_record_ids
    assert {audit.payload["raw_value"] for audit in raw_audits} == {
        first_record["raw_observed_at"],
        second_record["raw_observed_at"],
    }
    assert {audit.payload["raw_timezone"] for audit in raw_audits} == {
        "Asia/Shanghai" if region is Region.CN else "Asia/Hong_Kong"
    }


@pytest.mark.parametrize("region", [Region.CN, Region.HK])
async def test_prv_011_persists_unsupported_historical_pit_evidence(
    database: Database, region: Region
) -> None:
    service = IngestionCheckpointService()
    run_id = uuid4()
    provider_id = _provider_id(region)
    unit_of_work = UnitOfWork(database)

    with pytest.raises(UnsupportedCapabilityError):
        async with unit_of_work.transaction():
            await service.reject_unsupported_historical_pit(
                database,
                run_id=run_id,
                provider_id=provider_id,
                supports_point_in_time=False,
                historical_request=True,
            )

    async with database.session() as session:
        audit = await session.scalar(
            select(IngestAuditRow).where(
                IngestAuditRow.run_id == run_id,
                IngestAuditRow.audit_kind == "unsupported_historical_pit",
            )
        )
    assert audit is not None
    assert audit.provider_id == provider_id
    assert audit.payload == {"supports_point_in_time": False}


@pytest.mark.parametrize("region", [Region.CN, Region.HK])
async def test_prv_012_persists_raw_timezone_audit_evidence(
    database: Database, region: Region
) -> None:
    service = IngestionCheckpointService()
    run_id = uuid4()
    provider_id = _provider_id(region)
    unit_of_work = UnitOfWork(database)

    async with unit_of_work.transaction():
        await service.record_raw_timestamp(
            database,
            run_id=run_id,
            provider_id=provider_id,
            raw_value="2026-07-24 09:30:00",
            raw_timezone="Asia/Shanghai" if region is Region.CN else "Asia/Hong_Kong",
            normalized_utc="2026-07-24T01:30:00Z",
        )

    async with database.session() as session:
        audit = await session.scalar(
            select(IngestAuditRow).where(
                IngestAuditRow.run_id == run_id,
                IngestAuditRow.audit_kind == "raw_timestamp_normalization",
            )
        )
    assert audit is not None
    assert audit.payload == {
        "raw_value": "2026-07-24 09:30:00",
        "raw_timezone": "Asia/Shanghai" if region is Region.CN else "Asia/Hong_Kong",
        "normalized_utc": "2026-07-24T01:30:00Z",
    }


@pytest.mark.parametrize("region", [Region.CN, Region.HK])
async def test_prv_014_retry_after_lost_response_writes_page_once(
    database: Database, region: Region
) -> None:
    service = IngestionCheckpointService()
    unit_of_work = UnitOfWork(database)
    page = _page(region)
    observation_id = f"checkpoint-{uuid4().hex}"
    async with unit_of_work.transaction() as session:
        run_id = await _create_durable_run(
            session, region=region, dataset=Dataset.MARKET_OBSERVATIONS
        )

    async def persist_record(session: AsyncSession) -> None:
        session.add(
            MarketObservationRow(
                observation_id=observation_id,
                region=region.value,
                metric_code="market.checkpoint_test",
                scope_id="all",
                observed_at=datetime(2026, 7, 24, tzinfo=UTC),
                available_at=datetime(2026, 7, 24, tzinfo=UTC),
                provider_id=_provider_id(region),
                provider_record_id=page.accepted_record_ids[0],
                ingestion_run_id=run_id,
                payload={"purpose": "retry-contract"},
            )
        )

    async with unit_of_work.transaction() as session:
        assert await service.commit_page(
            IngestionCheckpointRepository(session, ingestion_run_id=run_id), page, persist_record
        )

    async def must_not_write(_: AsyncSession) -> None:
        raise AssertionError("a committed page must not invoke the record writer on retry")

    async with unit_of_work.transaction() as session:
        assert not await service.commit_page(
            IngestionCheckpointRepository(session, ingestion_run_id=run_id), page, must_not_write
        )

    async with database.session() as session:
        persisted_rows = await session.scalar(
            select(func.count())
            .select_from(MarketObservationRow)
            .where(MarketObservationRow.observation_id == observation_id)
        )
    assert persisted_rows == 1


@pytest.mark.parametrize("region", [Region.CN, Region.HK])
async def test_prv_016_recovers_committed_watermark_after_new_session(
    database: Database, region: Region
) -> None:
    service = IngestionCheckpointService()
    unit_of_work = UnitOfWork(database)
    page = _page(region, dataset=Dataset.INSTRUMENTS)
    async with unit_of_work.transaction() as session:
        run_id = await _create_durable_run(session, region=region, dataset=page.dataset)

    async def no_records(_: AsyncSession) -> None:
        return None

    async with unit_of_work.transaction() as session:
        assert await service.commit_page(
            IngestionCheckpointRepository(session, ingestion_run_id=run_id), page, no_records
        )

    context = FetchContext(
        request_id=uuid4(),
        as_of=datetime(2026, 7, 24, tzinfo=UTC),
        deadline_at=datetime(2026, 7, 24, 0, 1, tzinfo=UTC),
    )
    with pytest.raises(ProviderCursorError):
        await _provider_for(region).fetch_instruments(
            InstrumentQuery(regions={region}, cursor=page.next_cursor), context
        )

    async with database.session() as fresh_session:
        assert await service.recover_committed_watermark(
            IngestionCheckpointRepository(fresh_session, ingestion_run_id=run_id),
            provider_role=page.provider_role,
            dataset=page.dataset,
            region=page.region,
        ) == (page.source_watermark, page.next_cursor)


@pytest.mark.parametrize("region", [Region.CN, Region.HK])
async def test_prv_014_concurrent_retry_reserves_page_before_record_write(
    database: Database, region: Region
) -> None:
    service = IngestionCheckpointService()
    unit_of_work = UnitOfWork(database)
    page = _page(region)
    retry_started = Event()
    async with unit_of_work.transaction() as session:
        run_id = await _create_durable_run(
            session, region=region, dataset=Dataset.MARKET_OBSERVATIONS
        )

    async def first_write(_: AsyncSession) -> None:
        return None

    async def retry() -> bool:
        async with unit_of_work.transaction() as retry_session:
            retry_started.set()

            async def must_not_write(_: AsyncSession) -> None:
                raise AssertionError("a concurrent retry must not write a reserved page")

            return await service.commit_page(
                IngestionCheckpointRepository(retry_session, ingestion_run_id=run_id),
                page,
                must_not_write,
            )

    async with unit_of_work.transaction() as first_session:
        assert await service.commit_page(
            IngestionCheckpointRepository(first_session, ingestion_run_id=run_id), page, first_write
        )
        retry_task = create_task(retry())
        await wait_for(retry_started.wait(), timeout=1)
        assert not retry_task.done()

    assert not await wait_for(retry_task, timeout=1)
