from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from macro_platform.contracts.common import Region
from macro_platform.contracts.provider import Dataset, IngestJobRequest
from macro_platform.jobs import us_twelve_data_ingestion as ingestion_module
from macro_platform.jobs.ingestion_checkpoint import CommittedPage, IngestionCheckpointService
from macro_platform.jobs.runner import IngestionExecutionContext
from macro_platform.jobs.us_twelve_data_ingestion import UsTwelveDataIngestHandler
from macro_platform.providers.us.twelve_data import (
    TwelveDataDailyBarsProvider,
    TwelveDataInstrument,
)

NOW = datetime(2026, 7, 23, 21, tzinfo=UTC)
RUN_ID = UUID("00000000-0000-4000-8000-000000000034")
SPY = TwelveDataInstrument(
    instrument_id="ins_us_etf_spy",
    canonical_symbol="ARCX:SPY",
    source_symbol="SPY",
)


class _RecordingCheckpoints(IngestionCheckpointService):
    def __init__(self) -> None:
        self.historical_requests: list[bool] = []
        self.rejections: list[tuple[str, dict[str, object]]] = []
        self.recovered_checkpoint: tuple[str | None, str | None] = (None, None)

    async def reject_unsupported_historical_pit(
        self,
        database: object,
        *,
        run_id: UUID,
        provider_id: str,
        supports_point_in_time: bool,
        historical_request: bool,
    ) -> None:
        self.historical_requests.append(historical_request)
        assert historical_request is False

    async def record_raw_timestamp(
        self,
        database: object,
        *,
        run_id: UUID,
        provider_id: str,
        raw_value: str,
        raw_timezone: str,
        normalized_utc: str,
    ) -> None:
        return None

    async def record_rejection(
        self,
        database: object,
        *,
        run_id: UUID,
        provider_id: str,
        error_code: str,
        redacted_payload: dict[str, object],
    ) -> None:
        self.rejections.append((error_code, redacted_payload))

    async def commit_page(
        self,
        repository: _FakeRepository,
        page: CommittedPage,
        write_records: object,
    ) -> bool:
        await write_records(repository.session)  # type: ignore[operator]
        return True

    async def recover_committed_watermark(
        self,
        repository: _FakeRepository,
        *,
        provider_role: str,
        dataset: Dataset,
        region: str,
    ) -> tuple[str | None, str | None]:
        return self.recovered_checkpoint


class _FakeRepository:
    bars: list[object] = []
    instruments: list[object] = []

    def __init__(self, session: object, *, ingestion_run_id: UUID) -> None:
        self.session = session

    async def upsert_bar(self, bar: object) -> None:
        self.__class__.bars.append(bar)

    async def upsert_instrument(self, instrument: object) -> None:
        self.__class__.instruments.append(instrument)


class _FakeUnitOfWork:
    def __init__(self, database: object) -> None:
        self._database = database

    @asynccontextmanager
    async def transaction(self):
        yield object()


class _FakeDatabase:
    @asynccontextmanager
    async def session(self):
        yield object()


def _provider(payload: dict[str, object]) -> TwelveDataDailyBarsProvider:
    return TwelveDataDailyBarsProvider(
        api_key=SecretStr("ingestion-test-api-key"),
        instruments=[SPY],
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload, request=request)
            )
        ),
        cursor_signing_secret="ingestion-test-cursor-secret",
        clock=lambda: NOW,
    )


def _request() -> IngestJobRequest:
    return IngestJobRequest(
        provider_role="us.market.primary",
        dataset=Dataset.BARS,
        regions={Region.US},
        start=datetime(2026, 7, 22, 4, tzinfo=UTC),
        end=datetime(2026, 7, 23, 4, tzinfo=UTC),
        as_of=NOW - timedelta(milliseconds=1),
    )


@pytest.mark.asyncio
async def test_PIT_001_handler_uses_a_transport_deadline_not_the_business_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRepository.bars = []
    _FakeRepository.instruments = []
    provider = _provider(
        {
            "meta": {"symbol": "SPY", "interval": "1day", "currency": "USD"},
            "values": [
                {
                    "datetime": "2026-07-22",
                    "open": "610",
                    "high": "615",
                    "low": "609",
                    "close": "614",
                    "volume": "100",
                }
            ],
        }
    )
    monkeypatch.setattr(ingestion_module, "UnitOfWork", _FakeUnitOfWork)
    monkeypatch.setattr(ingestion_module, "IngestionCheckpointRepository", _FakeRepository)
    checkpoints = _RecordingCheckpoints()
    try:
        result = await UsTwelveDataIngestHandler(provider, now=lambda: NOW).run_checkpointed(
            _request(),
            checkpoints,
            object(),  # type: ignore[arg-type]
            IngestionExecutionContext(run_id=RUN_ID),
        )
    finally:
        await provider.aclose()

    assert checkpoints.historical_requests == [False]
    assert result.records_inserted == 1
    assert len(_FakeRepository.bars) == 1
    assert [instrument.local_symbol for instrument in _FakeRepository.instruments] == ["SPY"]


@pytest.mark.asyncio
async def test_PRV_009_handler_persists_quarantine_rejection_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRepository.bars = []
    _FakeRepository.instruments = []
    provider = _provider(
        {
            "meta": {"symbol": "SPY", "interval": "1day", "currency": "USD"},
            "values": [
                {
                    "datetime": "2026-07-22",
                    "open": "610",
                    "high": "615",
                    "low": "609",
                    "volume": "100",
                }
            ],
        }
    )
    monkeypatch.setattr(ingestion_module, "UnitOfWork", _FakeUnitOfWork)
    monkeypatch.setattr(ingestion_module, "IngestionCheckpointRepository", _FakeRepository)
    checkpoints = _RecordingCheckpoints()
    try:
        result = await UsTwelveDataIngestHandler(provider, now=lambda: NOW).run_checkpointed(
            _request(),
            checkpoints,
            object(),  # type: ignore[arg-type]
            IngestionExecutionContext(run_id=RUN_ID),
        )
    finally:
        await provider.aclose()

    assert result.records_rejected == 1
    assert checkpoints.rejections == [
        (
            "PROVIDER_SCHEMA_CHANGED",
            {"datetime": "2026-07-22", "fields": ["datetime", "high", "low", "open", "volume"]},
        )
    ]
    assert _FakeRepository.bars == []


@pytest.mark.asyncio
async def test_PRV_016_handler_recovers_from_the_committed_cursor_without_replaying_page_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRepository.bars = []
    _FakeRepository.instruments = []
    monkeypatch.setattr(ingestion_module, "_PAGE_SIZE", 1)
    monkeypatch.setattr(ingestion_module, "UnitOfWork", _FakeUnitOfWork)
    monkeypatch.setattr(ingestion_module, "IngestionCheckpointRepository", _FakeRepository)
    provider = _provider(
        {
            "meta": {"symbol": "SPY", "interval": "1day", "currency": "USD"},
            "values": [
                {
                    "datetime": "2026-07-21",
                    "open": "600",
                    "high": "605",
                    "low": "599",
                    "close": "604",
                    "volume": "100",
                },
                {
                    "datetime": "2026-07-22",
                    "open": "610",
                    "high": "615",
                    "low": "609",
                    "close": "614",
                    "volume": "200",
                },
            ],
        }
    )
    request = _request().model_copy(
        update={"start": datetime(2026, 7, 21, 4, tzinfo=UTC), "cursor": "expired"}
    )
    recovery_context = provider.request_timeout_seconds
    live_as_of = (
        request.as_of + timedelta(seconds=recovery_context) + ingestion_module._PIT_CLOCK_SKEW
    )
    first_page = await provider.fetch_bars(
        query=ingestion_module.BarQuery(
            instrument_ids=list(provider.instrument_ids),
            interval=ingestion_module.Interval.D1,
            start=request.start,
            end=request.end,
            adjustment=ingestion_module.Adjustment.RAW,
            as_of=live_as_of,
            limit=1,
        ),
        context=ingestion_module.FetchContext(
            request_id=RUN_ID,
            as_of=live_as_of,
            deadline_at=NOW + timedelta(seconds=recovery_context),
        ),
    )
    assert first_page.next_cursor is not None
    checkpoints = _RecordingCheckpoints()
    checkpoints.recovered_checkpoint = (first_page.source_watermark, first_page.next_cursor)
    try:
        result = await UsTwelveDataIngestHandler(provider, now=lambda: NOW).run_checkpointed(
            request,
            checkpoints,
            _FakeDatabase(),  # type: ignore[arg-type]
            IngestionExecutionContext(run_id=RUN_ID),
        )
    finally:
        await provider.aclose()

    assert result.records_inserted == 1
    assert [bar.trading_date.isoformat() for bar in _FakeRepository.bars] == ["2026-07-22"]
    assert [warning.code for warning in result.warnings] == ["CURSOR_RECOVERED"]
