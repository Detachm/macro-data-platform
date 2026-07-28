from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest

from macro_platform.contracts.common import Region
from macro_platform.jobs.scheduler import ScheduledTaskResult
from macro_platform.services.report_generator import ReportPromptBuilder
from macro_platform.services.report_input_materializer import (
    ExchangeMarketSessionCalendar,
    InputQualityEvidence,
    MaterializedReportInput,
    ReportInputSnapshotMaterializer,
    _unexpected_trading_dates,
)
from macro_platform.storage.reporting import ReportInputSnapshot

NOW = datetime(2026, 7, 28, 0, 0, tzinfo=UTC)
REPORT_DATE = date(2026, 7, 28)


@dataclass
class _EvidenceStore:
    evidence: tuple[InputQualityEvidence, ...]
    task_results: tuple[ScheduledTaskResult, ...] | None = None

    async def collect(
        self,
        *,
        report_date: date,
        as_of: datetime,
        cutoff_at: datetime,
        task_results: tuple[ScheduledTaskResult, ...],
    ) -> tuple[InputQualityEvidence, ...]:
        assert (report_date, as_of, cutoff_at) == (REPORT_DATE, NOW, NOW)
        self.task_results = task_results
        return self.evidence


@dataclass
class _SnapshotStore:
    saved: list[ReportInputSnapshot]

    async def put(self, snapshot: ReportInputSnapshot) -> None:
        self.saved.append(snapshot)


@pytest.mark.asyncio
async def test_rpt_029_materializer_derives_quality_and_persists_an_immutable_snapshot() -> None:
    evidence_store = _EvidenceStore(
        (
            InputQualityEvidence(
                input_id="market.cn.core_indices.previous_close",
                status="available",
                required=True,
                reason="three expected CN daily bars were available before cutoff",
                facts=(
                    {
                        "fact_id": "fact.market.cn.core_indices.previous_close",
                        "label": "CN core-index previous close",
                        "display_text": "CN core-index daily bars are available.",
                        "value": 3,
                        "unit": "instruments",
                        "available_at": NOW.isoformat().replace("+00:00", "Z"),
                        "report_date": REPORT_DATE.isoformat(),
                        "source_ref_ids": ["source-cn-1"],
                    },
                ),
                source_references=(
                    {
                        "source_ref_id": "source-cn-1",
                        "provider_id": "cn.baostock.v1",
                        "provider_record_id": "row-1",
                        "source_name": "BaoStock",
                        "source_url": "https://example.com/cn/row-1",
                        "retrieved_at": NOW.isoformat().replace("+00:00", "Z"),
                        "checksum_sha256": "a" * 64,
                    },
                ),
            ),
            *tuple(
                InputQualityEvidence(
                    input_id=input_id,
                    status="missing",
                    required=True,
                    reason="no materialized input facts",
                )
                for input_id in (
                    "market.hk.core_indices.previous_close",
                    "market.us.core_indices.previous_close",
                    "news.cn.official_headlines_24h",
                    "news.hk.official_headlines_24h",
                    "calendar.macro_releases_7d",
                )
            ),
        )
    )
    snapshot_store = _SnapshotStore([])
    materializer = ReportInputSnapshotMaterializer(
        evidence_store=evidence_store,
        snapshot_store=snapshot_store,
        now=lambda: NOW,
        cutoff_at=lambda _: NOW,
    )

    result = await materializer.materialize(
        REPORT_DATE,
        task_results=(
            ScheduledTaskResult(
                task_id="cn.daily-bars",
                provider_role="cn.bars.primary",
                status="succeeded",
            ),
        ),
    )

    assert isinstance(result, MaterializedReportInput)
    assert result.quality.status == "blocked"
    assert result.snapshot.fact_ids == ["fact.market.cn.core_indices.previous_close"]
    assert (
        result.snapshot.payload["input_quality"]["market.cn.core_indices.previous_close"]["status"]
        == "available"
    )
    assert result.snapshot.payload["source_ref_ids"] == ["source-cn-1"]
    assert result.snapshot.payload["editor_context"] == {
        "facts": result.snapshot.payload["facts"],
        "source_references": result.snapshot.payload["source_references"],
        "input_quality": result.snapshot.payload["input_quality"],
        "report_day_policy": result.snapshot.payload["report_day_policy"],
    }
    prompt = ReportPromptBuilder().build(result.snapshot, model="test-model", parameters={})
    assert prompt.source_ref_ids == ["source-cn-1"]
    assert prompt.input_payload["editor_context"] == result.snapshot.payload["editor_context"]
    assert "usage_rights" not in result.snapshot.payload
    assert snapshot_store.saved == [result.snapshot]


@pytest.mark.asyncio
async def test_rpt_051_weekend_snapshot_persists_policy_and_does_not_block_closed_markets() -> None:
    report_date = date(2026, 7, 26)
    as_of = datetime(2026, 7, 26, 0, 15, tzinfo=UTC)
    market_inputs = (
        "market.cn.core_indices.previous_close",
        "market.hk.core_indices.previous_close",
        "market.us.core_indices.previous_close",
    )
    always_required = (
        "news.cn.official_headlines_24h",
        "news.hk.official_headlines_24h",
        "calendar.macro_releases_7d",
    )

    class WeekendEvidenceStore:
        async def collect(self, **_kwargs: object) -> tuple[InputQualityEvidence, ...]:
            return (
                *tuple(
                    InputQualityEvidence(
                        input_id=input_id,
                        status="missing",
                        required=True,
                        reason="no current-session market facts",
                    )
                    for input_id in market_inputs
                ),
                *tuple(
                    InputQualityEvidence(
                        input_id=input_id,
                        status="available",
                        required=True,
                        reason="required weekend input is available",
                        facts=(
                            {
                                "fact_id": f"fact.{input_id}",
                                "available_at": as_of.isoformat().replace("+00:00", "Z"),
                                "source_ref_ids": [],
                            },
                        ),
                    )
                    for input_id in always_required
                ),
            )

    snapshot_store = _SnapshotStore([])
    result = await ReportInputSnapshotMaterializer(
        evidence_store=WeekendEvidenceStore(),
        snapshot_store=snapshot_store,
        now=lambda: as_of,
        cutoff_at=lambda _: as_of,
    ).materialize(report_date, task_results=())

    assert result.quality.status == "degraded"
    assert result.snapshot.payload["report_day_policy"]["day_type"] == "weekend"
    for input_id in market_inputs:
        assert result.snapshot.payload["input_quality"][input_id] == {
            "status": "unavailable",
            "required": False,
            "reason": (
                f"{input_id.split('.')[1].upper()} market is closed (weekend_closed); "
                "current-session market input is optional for this report date"
            ),
        }
    assert (
        result.snapshot.payload["editor_context"]["report_day_policy"]
        == result.snapshot.payload["report_day_policy"]
    )


@pytest.mark.asyncio
async def test_rpt_029_materializer_waits_until_cutoff_before_collecting_evidence() -> None:
    clock = {"now": NOW - timedelta(minutes=15)}
    delays: list[float] = []
    evidence_store = _EvidenceStore(())
    snapshot_store = _SnapshotStore([])

    async def advance_to_cutoff(delay: float) -> None:
        delays.append(delay)
        clock["now"] = NOW

    materializer = ReportInputSnapshotMaterializer(
        evidence_store=evidence_store,
        snapshot_store=snapshot_store,
        now=lambda: clock["now"],
        cutoff_at=lambda _: NOW,
        sleeper=advance_to_cutoff,
    )

    result = await materializer.materialize(REPORT_DATE, task_results=())

    assert delays == [900.0]
    assert result.snapshot.as_of == NOW
    assert evidence_store.task_results == ()
    assert snapshot_store.saved == [result.snapshot]


@pytest.mark.asyncio
async def test_rpt_029_materializer_excludes_late_evidence_from_the_snapshot() -> None:
    evidence_store = _EvidenceStore(
        (
            InputQualityEvidence(
                input_id="market.cn.core_indices.previous_close",
                status="available",
                required=True,
                reason="all required facts are materialized before cutoff",
                facts=(
                    {
                        "fact_id": "fact.market.cn.before-cutoff",
                        "value": "10.50",
                        "available_at": NOW.isoformat().replace("+00:00", "Z"),
                        "source_ref_ids": ["source-before-cutoff"],
                    },
                ),
                source_references=(
                    {
                        "source_ref_id": "source-before-cutoff",
                        "provider_id": "test.provider",
                    },
                ),
            ),
            InputQualityEvidence(
                input_id="news.hk.official_headlines_24h",
                status="available",
                required=True,
                reason="all required facts are materialized before cutoff",
                facts=(
                    {
                        "fact_id": "fact.news.hk.after-cutoff",
                        "value": "must not enter snapshot",
                        "available_at": (NOW.replace(hour=1)).isoformat().replace("+00:00", "Z"),
                        "source_ref_ids": ["source-after-cutoff"],
                    },
                ),
                source_references=(
                    {
                        "source_ref_id": "source-after-cutoff",
                        "provider_id": "test.provider",
                    },
                ),
            ),
        )
    )
    snapshot_store = _SnapshotStore([])
    materializer = ReportInputSnapshotMaterializer(
        evidence_store=evidence_store,
        snapshot_store=snapshot_store,
        now=lambda: NOW,
        cutoff_at=lambda _: NOW,
    )

    result = await materializer.materialize(REPORT_DATE, task_results=())

    assert result.snapshot.fact_ids == ["fact.market.cn.before-cutoff"]
    assert result.snapshot.payload["source_ref_ids"] == ["source-before-cutoff"]
    assert result.snapshot.payload["input_quality"]["news.hk.official_headlines_24h"] == {
        "status": "late",
        "required": True,
        "reason": "materialized fact became available after the report cutoff",
    }


@pytest.mark.parametrize(
    ("region", "report_date", "expected_session"),
    [
        (Region.CN, date(2026, 10, 8), date(2026, 9, 30)),
        (Region.HK, date(2026, 4, 7), date(2026, 4, 2)),
        (Region.US, date(2026, 7, 6), date(2026, 7, 2)),
    ],
)
def test_rpt_029_exchange_market_calendar_uses_the_previous_actual_session(
    region: Region, report_date: date, expected_session: date
) -> None:
    assert (
        ExchangeMarketSessionCalendar().previous_session(
            region=region,
            report_date=report_date,
        )
        == expected_session
    )


def test_rpt_029_recent_retrieval_cannot_mask_an_old_market_trading_session() -> None:
    expected_session = date(2026, 7, 27)

    stale_dates = _unexpected_trading_dates(
        (date(2026, 7, 24), date(2026, 7, 24), date(2026, 7, 24)),
        expected_trading_date=expected_session,
    )

    assert stale_dates == [date(2026, 7, 24)]
