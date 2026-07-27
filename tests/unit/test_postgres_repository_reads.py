from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from macro_platform.contracts.common import AssetClass, AvailabilityBasis, Region
from macro_platform.contracts.macro import (
    Frequency,
    MacroObservation,
    MacroObservationQuery,
    MacroRelease,
    MacroReleaseQuery,
    MacroSeries,
    MacroSeriesQuery,
    RevisionPolicy,
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
from macro_platform.contracts.news import ContentMode, EntityRef, NewsQuery, SourceTier
from macro_platform.contracts.provider import Dataset
from macro_platform.storage.models import ProviderRunRow
from macro_platform.storage.reporting import ReportInputSnapshot, StoredDailyReport
from macro_platform.storage.repositories import (
    IngestionRunRepository,
    NormalizedFactRepository,
    PostgresDataRepository,
)
from tests.helpers import NOW, news_event, source_ref


class _ScalarResult:
    def __init__(self, values: list[dict[str, Any]]) -> None:
        self._values = values

    def all(self) -> list[dict[str, Any]]:
        return self._values


class _ReadSession:
    def __init__(self, batches: deque[list[dict[str, Any]]]) -> None:
        self._batches = batches
        self.statements: list[object] = []

    async def scalars(self, statement: object) -> _ScalarResult:
        self.statements.append(statement)
        return _ScalarResult(self._batches.popleft())


class _ReadDatabase:
    def __init__(self, batches: list[list[dict[str, Any]]]) -> None:
        self.session_value = _ReadSession(deque(batches))

    @asynccontextmanager
    async def session(self) -> AsyncIterator[_ReadSession]:
        yield self.session_value


class _RecoveredRunRepository(IngestionRunRepository):
    def __init__(self, row: ProviderRunRow | None) -> None:
        self._row = row

    async def load_run(self, run_id: object) -> ProviderRunRow | None:
        return self._row


class _WriteResult:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> str | None:
        return self._value


class _WriteSession:
    def __init__(self, values: list[str | None]) -> None:
        self._values = deque(values)
        self.statement_count = 0

    async def execute(self, statement: object) -> _WriteResult:
        self.statement_count += 1
        return _WriteResult(self._values.popleft())


def _instrument() -> Instrument:
    return Instrument(
        instrument_id="ins-storage-read-1",
        canonical_symbol="XSHG:600000",
        region=Region.CN,
        venue_mic="XSHG",
        local_symbol="600000",
        name="存储读取测试标的",
        asset_class=AssetClass.EQUITY,
        currency="CNY",
        timezone="Asia/Shanghai",
        status=InstrumentStatus.ACTIVE,
        valid_from=date(2020, 1, 1),
        source=source_ref(),
    )


def _bar(instrument: Instrument) -> MarketBar:
    return MarketBar(
        bar_id="bar-storage-read-1",
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
        close=Decimal("10.5"),
        volume=Decimal("100"),
        currency="CNY",
        adjustment=Adjustment.RAW,
        available_at=NOW,
        availability_basis=AvailabilityBasis.EXCHANGE_PUBLISHED,
        source=source_ref(),
    )


async def test_sto_027_postgres_read_repository_rehydrates_all_public_contracts() -> None:
    instrument = _instrument()
    bar = _bar(instrument)
    market_observation = MarketObservation(
        observation_id="market-observation-storage-read-1",
        region=Region.CN,
        scope_type=ScopeType.MARKET,
        scope_id="CN",
        metric_code="market.turnover",
        value=Decimal("100"),
        unit="CNY",
        period_start=NOW - timedelta(hours=1),
        period_end=NOW,
        observed_at=NOW,
        available_at=NOW,
        availability_basis=AvailabilityBasis.FIRST_SEEN,
        source=source_ref(),
    )
    series = MacroSeries(
        series_id="macro:CN:TEST:STORAGE_READ",
        region=Region.CN,
        authority="Storage test authority",
        code="STORAGE_READ",
        name="Storage read series",
        frequency=Frequency.MONTHLY,
        unit="index",
        transformation="level",
        seasonal_adjustment="not_adjusted",
        source=source_ref(),
    )
    macro_observation = MacroObservation(
        observation_id="macro-observation-storage-read-1",
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
        value_status="final",
        source=source_ref(),
    )
    release = MacroRelease(
        release_id="macro-release-storage-read-1",
        series_id=series.series_id,
        region=Region.CN,
        release_name="Storage read release",
        scheduled_at=NOW + timedelta(days=1),
        available_at=NOW,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        unit="index",
        status="scheduled",
        source=source_ref(),
    )
    event = news_event().model_copy(
        update={
            "entities": [
                EntityRef(
                    entity_type="country",
                    entity_id="CN",
                    mention="中国",
                    confidence=Decimal("1"),
                )
            ],
            "topics": ["macro"],
        }
    )
    payloads = [
        [instrument.model_dump(mode="json")],
        [bar.model_dump(mode="json")],
        [bar.model_dump(mode="json")],
        [market_observation.model_dump(mode="json")],
        [series.model_dump(mode="json")],
        [macro_observation.model_dump(mode="json")],
        [macro_observation.model_dump(mode="json")],
        [macro_observation.model_dump(mode="json")],
        [release.model_dump(mode="json")],
        [event.model_dump(mode="json")],
        [event.model_dump(mode="json")],
    ]
    database = _ReadDatabase(payloads)
    repository = PostgresDataRepository(database)  # type: ignore[arg-type]
    as_of = NOW + timedelta(hours=1)

    assert await repository.list_instruments(
        InstrumentQuery(
            regions={Region.CN},
            venues={instrument.venue_mic},
            asset_classes={instrument.asset_class},
            active_on=NOW.date(),
            modified_since=NOW - timedelta(days=1),
        )
    ) == [instrument]
    assert await repository.list_bars(
        BarQuery(
            instrument_ids=[instrument.instrument_id],
            interval=Interval.D1,
            start=bar.bar_start,
            end=NOW + timedelta(seconds=1),
            as_of=as_of,
        )
    ) == [bar]
    assert (
        await repository.list_snapshots(
            MarketSnapshotQuery(instrument_ids=[instrument.instrument_id], as_of=as_of)
        )
    )[0].last == bar.close
    assert await repository.list_market_observations(
        MarketObservationQuery(
            regions={Region.CN},
            metric_codes=[market_observation.metric_code],
            scope_ids=[market_observation.scope_id],
            start=NOW - timedelta(hours=1),
            end=NOW + timedelta(seconds=1),
            as_of=as_of,
        )
    ) == [market_observation]
    assert await repository.list_macro_series(
        MacroSeriesQuery(regions={Region.CN}, series_ids=[series.series_id])
    ) == [series]
    for revision_policy in (
        RevisionPolicy.LATEST_AS_OF,
        RevisionPolicy.ALL_VINTAGES,
        RevisionPolicy.FIRST_RELEASE,
    ):
        assert await repository.list_macro_observations(
            MacroObservationQuery(
                series_ids=[series.series_id],
                period_from=macro_observation.period_end,
                period_to=macro_observation.period_end,
                as_of=as_of,
                revision_policy=revision_policy,
            )
        ) == [macro_observation]
    assert await repository.list_macro_releases(
        MacroReleaseQuery(
            regions={Region.CN},
            scheduled_from=NOW,
            scheduled_to=NOW + timedelta(days=2),
            as_of=as_of,
        )
    ) == [release]
    headline = await repository.list_news(
        NewsQuery(
            regions={Region.CN},
            published_from=NOW - timedelta(hours=1),
            published_to=NOW + timedelta(hours=1),
            as_of=as_of,
            entity_ids=["CN"],
            topics=["macro"],
            languages={event.language},
            source_tiers={SourceTier.OFFICIAL},
            include_superseded=True,
            content_mode=ContentMode.HEADLINE,
        )
    )
    snippet = await repository.list_news(
        NewsQuery(
            regions={Region.CN},
            published_from=NOW - timedelta(hours=1),
            published_to=NOW + timedelta(hours=1),
            as_of=as_of,
            content_mode=ContentMode.SNIPPET,
        )
    )

    assert headline[0].summary is None
    assert headline[0].body is None
    assert snippet[0].summary == event.summary
    assert snippet[0].body is None
    assert len(database.session_value.statements) == 11


async def test_sto_027_completed_ingestion_run_rehydrates_after_restart() -> None:
    run_id = uuid4()
    row = ProviderRunRow(
        run_id=run_id,
        idempotency_key="ingest-read-recovery",
        provider_role="cn.contract_fixture.market_observations",
        dataset=Dataset.MARKET_OBSERVATIONS.value,
        status="succeeded",
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        records_fetched=1,
        records_accepted=1,
        records_rejected=0,
        error_code=None,
        details={
            "records_inserted": 1,
            "records_updated": 0,
            "next_cursor": None,
            "source_watermark": "watermark-1",
            "retry_after_seconds": None,
            "warnings": [],
        },
        request_payload={"dataset": Dataset.MARKET_OBSERVATIONS.value},
    )
    repository = _RecoveredRunRepository(row)

    result = await repository.load_completed_result(run_id)

    assert result is not None
    assert result.run_id == run_id
    assert result.records_inserted == 1
    assert result.source_watermark == "watermark-1"
    assert await _RecoveredRunRepository(None).load_completed_result(run_id) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"report_id": "different", "input_snapshot": {"snapshot_id": "snapshot-1"}},
        {"report_id": "report-1", "input_snapshot": {"snapshot_id": "different"}},
    ],
)
def test_rep_027_report_storage_rejects_payload_identity_mismatch(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="storage identity"):
        StoredDailyReport(
            report_id="report-1",
            report_date=NOW.date(),
            report_version="v1",
            contract_version="1.0",
            input_snapshot_id="snapshot-1",
            status="complete",
            publication_decision="published",
            generated_at=NOW,
            payload=payload,
        )


def test_rep_027_report_storage_extracts_indexable_source_references() -> None:
    report = StoredDailyReport(
        report_id="report-1",
        report_date=NOW.date(),
        report_version="v1",
        contract_version="1.0",
        input_snapshot_id="snapshot-1",
        status="complete",
        publication_decision="published",
        generated_at=NOW,
        payload={
            "report_id": "report-1",
            "report_date": NOW.date().isoformat(),
            "contract_version": "1.0",
            "status": "complete",
            "publication": {"decision": "published"},
            "input_snapshot": {"snapshot_id": "snapshot-1"},
            "sections": {
                "source_references": {
                    "items": [
                        {
                            "source_ref_id": "source-1",
                            "provider_id": "fixture.provider",
                            "provider_record_id": "record-1",
                            "source_name": "Fixture source",
                            "source_url": "https://example.com/source-1",
                            "retrieved_at": NOW.isoformat(),
                            "checksum_sha256": "a" * 64,
                            "external_llm_allowed": False,
                        }
                    ]
                }
            },
        },
    )

    assert report.source_references()[0].provider_record_id == "record-1"


def test_rep_027_snapshot_storage_requires_the_frozen_input_contract() -> None:
    payload = {
        "snapshot_id": "snapshot-1",
        "snapshot_version": "1.0",
        "as_of": NOW.isoformat().replace("+00:00", "Z"),
        "cutoff_at": NOW.isoformat().replace("+00:00", "Z"),
        "fingerprint_sha256": "a" * 64,
        "fact_ids": ["fact-1"],
    }
    snapshot = ReportInputSnapshot(
        snapshot_id="snapshot-1",
        snapshot_version="1.0",
        report_date=NOW.date(),
        as_of=NOW,
        cutoff_at=NOW,
        fingerprint_sha256="a" * 64,
        fact_ids=["fact-1"],
        payload=payload,
    )

    assert snapshot.payload == payload
    with pytest.raises(ValueError, match="input facts"):
        ReportInputSnapshot(
            **snapshot.model_dump(exclude={"payload"}),
            payload={**payload, "fact_ids": []},
        )


async def test_sto_027_macro_release_keeps_pit_versions_without_in_place_update() -> None:
    release = MacroRelease(
        release_id="macro-release-storage-write-1",
        series_id="macro:CN:TEST:STORAGE_WRITE",
        region=Region.CN,
        release_name="Storage write release",
        scheduled_at=NOW + timedelta(days=1),
        available_at=NOW,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        unit="index",
        status="scheduled",
        source=source_ref(),
    )
    session = _WriteSession([release.release_id, None, None])
    repository = NormalizedFactRepository(session)  # type: ignore[arg-type]

    assert repository.session is session
    await repository.upsert_macro_release(release)
    await repository.upsert_macro_release(
        release.model_copy(
            update={
                "available_at": NOW + timedelta(hours=1),
                "released_at": NOW + timedelta(hours=1),
                "actual": Decimal("100"),
                "status": "released",
                "source": source_ref().model_copy(update={"checksum_sha256": "b" * 64}),
            }
        )
    )

    assert session.statement_count == 3
