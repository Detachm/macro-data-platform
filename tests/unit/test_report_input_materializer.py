from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

import pytest

from macro_platform.contracts.common import Region
from macro_platform.jobs.scheduler import ScheduledTaskResult
from macro_platform.services.report_generator import ReportPromptBuilder
from macro_platform.services.report_input_materializer import (
    InputQualityEvidence,
    MaterializedReportInput,
    ReportInputSnapshotMaterializer,
    WeekdayMarketSessionCalendar,
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
    }
    prompt = ReportPromptBuilder().build(result.snapshot, model="test-model", parameters={})
    assert prompt.source_ref_ids == ["source-cn-1"]
    assert prompt.input_payload["editor_context"] == result.snapshot.payload["editor_context"]
    assert "usage_rights" not in result.snapshot.payload
    assert snapshot_store.saved == [result.snapshot]


@pytest.mark.parametrize(
    ("report_date", "expected_session"),
    [
        (date(2026, 7, 28), date(2026, 7, 27)),
        (date(2026, 7, 27), date(2026, 7, 24)),
        (date(2026, 7, 26), date(2026, 7, 24)),
    ],
)
def test_rpt_029_weekday_market_calendar_uses_the_previous_effective_session(
    report_date: date, expected_session: date
) -> None:
    assert (
        WeekdayMarketSessionCalendar().previous_session(
            region=Region.CN,
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
