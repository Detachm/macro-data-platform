from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from macro_platform.contracts.macro import (
    MacroObservation,
    MacroObservationQuery,
    MacroRelease,
    MacroReleaseQuery,
    MacroSeries,
    MacroSeriesQuery,
)
from macro_platform.contracts.market import (
    BarQuery,
    Instrument,
    InstrumentQuery,
    MarketBar,
    MarketObservation,
    MarketObservationQuery,
    MarketSnapshot,
    MarketSnapshotQuery,
)
from macro_platform.contracts.news import NewsEvent, NewsQuery
from macro_platform.contracts.provider import Dataset
from macro_platform.storage.models import (
    IngestAuditRow,
    IngestPageCommitRow,
    JobWatermarkRow,
    MarketObservationRow,
)


class DataRepository(Protocol):
    async def list_instruments(self, query: InstrumentQuery) -> list[Instrument]: ...

    async def list_bars(self, query: BarQuery) -> list[MarketBar]: ...

    async def list_snapshots(self, query: MarketSnapshotQuery) -> list[MarketSnapshot]: ...

    async def list_market_observations(
        self, query: MarketObservationQuery
    ) -> list[MarketObservation]: ...

    async def list_macro_series(self, query: MacroSeriesQuery) -> list[MacroSeries]: ...

    async def list_macro_observations(
        self, query: MacroObservationQuery
    ) -> list[MacroObservation]: ...

    async def list_macro_releases(self, query: MacroReleaseQuery) -> list[MacroRelease]: ...

    async def list_news(self, query: NewsQuery) -> list[NewsEvent]: ...


class EmptyDataRepository:
    """Development scaffold. Replace with PostgreSQL repositories dataset by dataset."""

    async def list_instruments(self, query: InstrumentQuery) -> list[Instrument]:
        return []

    async def list_bars(self, query: BarQuery) -> list[MarketBar]:
        return []

    async def list_snapshots(self, query: MarketSnapshotQuery) -> list[MarketSnapshot]:
        return []

    async def list_market_observations(
        self, query: MarketObservationQuery
    ) -> list[MarketObservation]:
        return []

    async def list_macro_series(self, query: MacroSeriesQuery) -> list[MacroSeries]:
        return []

    async def list_macro_observations(self, query: MacroObservationQuery) -> list[MacroObservation]:
        return []

    async def list_macro_releases(self, query: MacroReleaseQuery) -> list[MacroRelease]:
        return []

    async def list_news(self, query: NewsQuery) -> list[NewsEvent]:
        return []


class IngestionCheckpointRepository:
    """Database boundary for durable ingest audit and checkpoint records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    def add_audit(
        self, *, run_id: Any, provider_id: str, audit_kind: str, payload: dict[str, Any]
    ) -> None:
        self._session.add(
            IngestAuditRow(
                run_id=run_id,
                provider_id=provider_id,
                audit_kind=audit_kind,
                payload=payload,
            )
        )

    async def reserve_page(
        self,
        *,
        provider_role: str,
        dataset: Dataset,
        region: str,
        page_fingerprint: str,
        source_watermark: str | None,
        next_cursor: str | None,
        accepted_record_ids: list[str],
    ) -> bool:
        reservation = await self._session.execute(
            insert(IngestPageCommitRow)
            .values(
                provider_role=provider_role,
                dataset=dataset.value,
                region=region,
                page_fingerprint=page_fingerprint,
                source_watermark=source_watermark,
                next_cursor=next_cursor,
                accepted_record_ids=accepted_record_ids,
            )
            .on_conflict_do_nothing(
                index_elements=(
                    IngestPageCommitRow.provider_role,
                    IngestPageCommitRow.dataset,
                    IngestPageCommitRow.region,
                    IngestPageCommitRow.page_fingerprint,
                )
            )
            .returning(IngestPageCommitRow.provider_role)
        )
        return reservation.scalar_one_or_none() is not None

    async def save_watermark(
        self,
        *,
        provider_role: str,
        dataset: Dataset,
        region: str,
        watermark: str | None,
        cursor: str | None,
    ) -> None:
        existing = await self._session.get(JobWatermarkRow, (provider_role, dataset.value, region))
        if existing is None:
            self._session.add(
                JobWatermarkRow(
                    provider_role=provider_role,
                    dataset=dataset.value,
                    region=region,
                    watermark=watermark,
                    cursor=cursor,
                )
            )
            return
        existing.watermark = watermark
        existing.cursor = cursor

    async def load_watermark(
        self, *, provider_role: str, dataset: Dataset, region: str
    ) -> tuple[str | None, str | None]:
        checkpoint = await self._session.scalar(
            select(JobWatermarkRow).where(
                JobWatermarkRow.provider_role == provider_role,
                JobWatermarkRow.dataset == dataset.value,
                JobWatermarkRow.region == region,
            )
        )
        return (None, None) if checkpoint is None else (checkpoint.watermark, checkpoint.cursor)

    async def upsert_market_observation(self, observation: MarketObservation) -> None:
        await self._session.execute(
            insert(MarketObservationRow)
            .values(
                observation_id=observation.observation_id,
                region=observation.region.value,
                metric_code=observation.metric_code,
                scope_id=observation.scope_id,
                observed_at=observation.observed_at,
                available_at=observation.available_at,
                provider_id=observation.source.provider_id,
                provider_record_id=observation.source.provider_record_id,
                payload=observation.model_dump(mode="json"),
            )
            .on_conflict_do_update(
                index_elements=(MarketObservationRow.observation_id,),
                set_={
                    "available_at": observation.available_at,
                    "provider_record_id": observation.source.provider_record_id,
                    "payload": observation.model_dump(mode="json"),
                },
            )
        )
