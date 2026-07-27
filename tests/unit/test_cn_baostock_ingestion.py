from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

import pytest

from macro_platform.contracts.common import Region
from macro_platform.contracts.provider import Dataset, IngestJobRequest
from macro_platform.jobs import cn_baostock_ingestion as ingestion_module
from macro_platform.jobs.cn_baostock_ingestion import CnBaoStockIngestHandler
from macro_platform.jobs.ingestion_checkpoint import CommittedPage, IngestionCheckpointService
from macro_platform.jobs.runner import IngestionExecutionContext
from macro_platform.providers.cn.baostock import BaoStockDailyBarsProvider, BaoStockInstrument

NOW = datetime(2026, 7, 23, 21, tzinfo=UTC)
RUN_ID = UUID("00000000-0000-4000-8000-000000000028")
CSI300 = BaoStockInstrument(
    instrument_id="ins_cn_index_csi300",
    canonical_symbol="XSHG:000300",
    source_symbol="sh.000300",
)
FIELDS = [
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "pctChg",
]


class _Result:
    error_code = "0"
    error_msg = "success"
    fields = FIELDS

    def __init__(self, rows: list[list[str]] | None = None) -> None:
        self._rows = [] if rows is None else rows
        self._index = 0

    def next(self) -> bool:
        if self._index >= len(self._rows):
            return False
        self._index += 1
        return True

    def get_row_data(self) -> list[str]:
        return self._rows[self._index - 1]


class _Client:
    def __init__(self, rows: list[list[str]]) -> None:
        self._rows = rows

    def login(self) -> _Result:
        return _Result()

    def logout(self) -> _Result:
        return _Result()

    def query_history_k_data_plus(self, *_: object, **__: object) -> _Result:
        return _Result(self._rows)


class _RecordingCheckpoints(IngestionCheckpointService):
    def __init__(self) -> None:
        self.historical_requests: list[bool] = []
        self.raw_timestamps: list[tuple[str, str, str]] = []
        self.rejections: list[tuple[str, dict[str, object]]] = []

    async def reject_unsupported_historical_pit(self, database: object, **kwargs: object) -> None:
        self.historical_requests.append(bool(kwargs["historical_request"]))

    async def record_raw_timestamp(self, database: object, **kwargs: object) -> None:
        self.raw_timestamps.append(
            (
                str(kwargs["raw_value"]),
                str(kwargs["raw_timezone"]),
                str(kwargs["normalized_utc"]),
            )
        )

    async def record_rejection(self, database: object, **kwargs: object) -> None:
        payload = kwargs["redacted_payload"]
        assert isinstance(payload, dict)
        self.rejections.append((str(kwargs["error_code"]), payload))

    async def commit_page(
        self, repository: _FakeRepository, page: CommittedPage, write_records: object
    ) -> bool:
        await write_records(repository.session)  # type: ignore[operator]
        return True


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


def _provider(rows: list[list[str]]) -> BaoStockDailyBarsProvider:
    return BaoStockDailyBarsProvider(
        instruments=[CSI300],
        client=_Client(rows),
        cursor_signing_secret="ingestion-test-cursor-secret",
        clock=lambda: NOW,
    )


def _request() -> IngestJobRequest:
    return IngestJobRequest(
        provider_role="cn.bars.primary",
        dataset=Dataset.BARS,
        regions={Region.CN},
        start=datetime(2026, 7, 22, 16, tzinfo=UTC),
        end=datetime(2026, 7, 23, 16, tzinfo=UTC),
        as_of=NOW,
    )


def _row(*, high: str = "4700.0", low: str = "4650.0") -> list[str]:
    return [
        "2026-07-23",
        "sh.000300",
        "4680.0",
        high,
        low,
        "4690.0",
        "4670.0",
        "123456",
        "789012.5",
        "0.43",
    ]


@pytest.mark.asyncio
async def test_PRV_016_baostock_handler_persists_cn_bar_instrument_and_raw_time_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRepository.bars = []
    _FakeRepository.instruments = []
    monkeypatch.setattr(ingestion_module, "UnitOfWork", _FakeUnitOfWork)
    monkeypatch.setattr(ingestion_module, "IngestionCheckpointRepository", _FakeRepository)
    checkpoints = _RecordingCheckpoints()

    result = await CnBaoStockIngestHandler(_provider([_row()]), now=lambda: NOW).run_checkpointed(
        _request(),
        checkpoints,
        object(),  # type: ignore[arg-type]
        IngestionExecutionContext(run_id=RUN_ID),
    )

    assert checkpoints.historical_requests == [False]
    assert result.records_inserted == 1
    assert len(_FakeRepository.bars) == 1
    assert [instrument.local_symbol for instrument in _FakeRepository.instruments] == ["000300"]
    assert checkpoints.raw_timestamps == [
        ("2026-07-23", "Asia/Shanghai", "2026-07-23T07:00:00+00:00")
    ]


@pytest.mark.asyncio
async def test_PRV_009_baostock_handler_persists_quarantine_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRepository.bars = []
    _FakeRepository.instruments = []
    monkeypatch.setattr(ingestion_module, "UnitOfWork", _FakeUnitOfWork)
    monkeypatch.setattr(ingestion_module, "IngestionCheckpointRepository", _FakeRepository)
    checkpoints = _RecordingCheckpoints()

    result = await CnBaoStockIngestHandler(
        _provider([_row(high="4700.0", low="4800.0")]), now=lambda: NOW
    ).run_checkpointed(
        _request(),
        checkpoints,
        object(),  # type: ignore[arg-type]
        IngestionExecutionContext(run_id=RUN_ID),
    )

    assert result.records_rejected == 1
    assert _FakeRepository.bars == []
    assert checkpoints.raw_timestamps == []
    assert checkpoints.rejections[0][0] == "SCHEMA_DRIFT"
