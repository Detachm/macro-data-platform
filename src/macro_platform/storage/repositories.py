from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Protocol
from typing import cast as typing_cast
from uuid import UUID

from sqlalchemy import DateTime, cast, delete, func, literal, or_, select, union_all, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Subquery

from macro_platform.contracts.common import WarningItem
from macro_platform.contracts.macro import (
    MacroObservation,
    MacroObservationQuery,
    MacroRelease,
    MacroReleaseQuery,
    MacroSeries,
    MacroSeriesQuery,
)
from macro_platform.contracts.market import (
    Adjustment,
    BarQuery,
    Instrument,
    InstrumentQuery,
    Interval,
    MarketBar,
    MarketObservation,
    MarketObservationQuery,
    MarketSnapshot,
    MarketSnapshotQuery,
)
from macro_platform.contracts.news import ContentMode, NewsEvent, NewsQuery
from macro_platform.contracts.provider import Dataset, IngestJobRequest, IngestJobResult
from macro_platform.normalization.common import stable_id
from macro_platform.storage.database import Database
from macro_platform.storage.models import (
    DailyReportRow,
    DailyReportSourceRefRow,
    DeliveryAttemptRow,
    IngestAuditRow,
    IngestPageCommitRow,
    IngestRejectionRow,
    InstrumentAliasRow,
    InstrumentRow,
    JobWatermarkRow,
    MacroObservationRow,
    MacroReleaseRevisionRow,
    MacroReleaseRow,
    MacroSeriesRow,
    MarketBarRevisionRow,
    MarketBarRow,
    MarketObservationRow,
    NewsEventEntityRow,
    NewsEventRegionRow,
    NewsEventRow,
    NewsEventTopicRow,
    ProviderRunRow,
    ReportGenerationAttemptRow,
    ReportInputSnapshotRow,
    ScheduledTaskCheckpointRow,
)
from macro_platform.storage.reporting import (
    DeliveryAttempt,
    ReportGenerationAttempt,
    ReportInputSnapshot,
    StoredDailyReport,
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


class NormalizedFactRepository:
    """Transaction-scoped writes for normalized facts and their provenance payloads."""

    def __init__(self, session: AsyncSession, *, ingestion_run_id: UUID) -> None:
        self._session = session
        self._ingestion_run_id = ingestion_run_id

    @property
    def session(self) -> AsyncSession:
        return self._session

    async def upsert_instrument(self, instrument: Instrument) -> None:
        payload = instrument.model_dump(mode="json")
        await self._session.execute(
            insert(InstrumentRow)
            .values(
                instrument_id=instrument.instrument_id,
                canonical_symbol=instrument.canonical_symbol,
                region=instrument.region.value,
                status=instrument.status.value,
                valid_from=instrument.valid_from,
                valid_to=instrument.valid_to,
                ingestion_run_id=self._ingestion_run_id,
                payload=payload,
            )
            .on_conflict_do_update(
                index_elements=(InstrumentRow.instrument_id,),
                set_={
                    "canonical_symbol": instrument.canonical_symbol,
                    "region": instrument.region.value,
                    "status": instrument.status.value,
                    "valid_from": instrument.valid_from,
                    "valid_to": instrument.valid_to,
                    "ingestion_run_id": self._ingestion_run_id,
                    "payload": payload,
                    "updated_at": func.now(),
                },
            )
        )
        source_symbol = instrument.source.source_symbol or instrument.local_symbol
        await self._session.execute(
            insert(InstrumentAliasRow)
            .values(
                instrument_id=instrument.instrument_id,
                provider_id=instrument.source.provider_id,
                source_symbol=source_symbol,
                valid_from=instrument.valid_from,
                valid_to=instrument.valid_to,
            )
            .on_conflict_do_update(
                constraint="uq_instrument_alias_effective",
                set_={
                    "instrument_id": instrument.instrument_id,
                    "valid_to": instrument.valid_to,
                },
            )
        )

    async def upsert_bar(self, bar: MarketBar) -> None:
        payload = bar.model_dump(mode="json")
        inserted = await self._session.execute(
            insert(MarketBarRow)
            .values(
                bar_id=bar.bar_id,
                instrument_id=bar.instrument_id,
                canonical_symbol=bar.canonical_symbol,
                region=bar.region.value,
                interval=bar.interval.value,
                bar_start=bar.bar_start,
                bar_end=bar.bar_end,
                trading_date=bar.trading_date,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                available_at=bar.available_at,
                adjustment=bar.adjustment.value,
                provider_id=bar.source.provider_id,
                provider_record_id=bar.source.provider_record_id,
                ingestion_run_id=self._ingestion_run_id,
                payload=payload,
            )
            .on_conflict_do_nothing(index_elements=(MarketBarRow.bar_id,))
            .returning(MarketBarRow.bar_id)
        )
        if inserted.scalar_one_or_none() is not None:
            return
        current_checksum = await self._session.scalar(
            self._current_bar_checksum_statement(bar.bar_id)
        )
        if current_checksum == bar.source.checksum_sha256:
            return
        if not isinstance(current_checksum, str):
            raise RuntimeError("persisted market bar is missing its source checksum")
        await self._session.execute(
            insert(MarketBarRevisionRow)
            .values(
                revision_id=stable_id(
                    "market-bar-revision",
                    bar.bar_id,
                    bar.available_at.isoformat(),
                    bar.source.checksum_sha256,
                ),
                bar_id=bar.bar_id,
                available_at=bar.available_at,
                source_checksum_sha256=bar.source.checksum_sha256,
                ingestion_run_id=self._ingestion_run_id,
                payload=payload,
            )
            .on_conflict_do_nothing(constraint="uq_market_bar_revision")
        )

    @staticmethod
    def _current_bar_checksum_statement(bar_id: str):  # type: ignore[no-untyped-def]
        """Read the checksum of the latest persisted version for one immutable bar."""

        base_version = select(
            MarketBarRow.available_at.label("available_at"),
            literal(0).label("revision_rank"),
            MarketBarRow.payload["source"]["checksum_sha256"].as_string().label("checksum"),
        ).where(MarketBarRow.bar_id == bar_id)
        revision_versions = select(
            MarketBarRevisionRow.available_at.label("available_at"),
            literal(1).label("revision_rank"),
            MarketBarRevisionRow.source_checksum_sha256.label("checksum"),
        ).where(MarketBarRevisionRow.bar_id == bar_id)
        versions = union_all(base_version, revision_versions).subquery()
        return (
            select(versions.c.checksum)
            .order_by(
                versions.c.available_at.desc(),
                versions.c.revision_rank.desc(),
                versions.c.checksum.desc(),
            )
            .limit(1)
        )

    async def upsert_market_observation(self, observation: MarketObservation) -> None:
        payload = observation.model_dump(mode="json")
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
                ingestion_run_id=self._ingestion_run_id,
                payload=payload,
            )
            .on_conflict_do_update(
                index_elements=(MarketObservationRow.observation_id,),
                set_={
                    "available_at": observation.available_at,
                    "provider_record_id": observation.source.provider_record_id,
                    "ingestion_run_id": self._ingestion_run_id,
                    "payload": payload,
                },
            )
        )

    async def upsert_macro_series(self, series: MacroSeries) -> None:
        payload = series.model_dump(mode="json")
        await self._session.execute(
            insert(MacroSeriesRow)
            .values(
                series_id=series.series_id,
                region=series.region.value,
                ingestion_run_id=self._ingestion_run_id,
                payload=payload,
            )
            .on_conflict_do_update(
                index_elements=(MacroSeriesRow.series_id,),
                set_={
                    "region": series.region.value,
                    "ingestion_run_id": self._ingestion_run_id,
                    "payload": payload,
                },
            )
        )

    async def upsert_macro_observation(self, observation: MacroObservation) -> None:
        payload = observation.model_dump(mode="json")
        await self._session.execute(
            insert(MacroObservationRow)
            .values(
                observation_id=observation.observation_id,
                series_id=observation.series_id,
                region=observation.region.value,
                period_end=observation.period_end,
                available_at=observation.available_at,
                vintage_id=observation.vintage_id,
                revision_no=observation.revision_no,
                provider_id=observation.source.provider_id,
                ingestion_run_id=self._ingestion_run_id,
                payload=payload,
            )
            # Macro revisions carry a new vintage/observation identity. A raw
            # replay with the same canonical ID must not replace a vintage.
            .on_conflict_do_nothing()
        )

    async def upsert_macro_release(self, release: MacroRelease) -> None:
        payload = release.model_dump(mode="json")
        inserted = await self._session.execute(
            insert(MacroReleaseRow)
            .values(
                release_id=release.release_id,
                series_id=release.series_id,
                region=release.region.value,
                scheduled_at=release.scheduled_at,
                scheduled_date=release.scheduled_date,
                available_at=release.available_at,
                source_checksum_sha256=release.source.checksum_sha256,
                ingestion_run_id=self._ingestion_run_id,
                payload=payload,
            )
            .on_conflict_do_nothing(index_elements=(MacroReleaseRow.release_id,))
            .returning(MacroReleaseRow.release_id)
        )
        if inserted.scalar_one_or_none() is not None:
            return
        existing_checksum = await self._session.scalar(
            select(MacroReleaseRow.source_checksum_sha256).where(
                MacroReleaseRow.release_id == release.release_id
            )
        )
        if existing_checksum == release.source.checksum_sha256:
            return
        await self._session.execute(
            insert(MacroReleaseRevisionRow)
            .values(
                revision_id=stable_id(
                    "macro-release-revision", release.release_id, release.source.checksum_sha256
                ),
                release_id=release.release_id,
                available_at=release.available_at,
                source_checksum_sha256=release.source.checksum_sha256,
                ingestion_run_id=self._ingestion_run_id,
                payload=payload,
            )
            .on_conflict_do_nothing(constraint="uq_macro_release_revision")
        )

    async def upsert_news_event(self, event: NewsEvent) -> None:
        payload = event.model_dump(mode="json")
        inserted = await self._session.execute(
            insert(NewsEventRow)
            .values(
                news_id=event.news_id,
                cluster_id=event.cluster_id,
                provider_id=event.source.provider_id,
                provider_record_id=event.source.provider_record_id,
                published_at=event.published_at,
                published_date=event.published_date,
                available_at=event.available_at,
                status=event.status,
                ingestion_run_id=self._ingestion_run_id,
                payload=payload,
            )
            # A correction is a new NewsEvent linked by supersedes_news_id;
            # never rewrite an already persisted event on replay.
            .on_conflict_do_nothing(index_elements=(NewsEventRow.news_id,))
            .returning(NewsEventRow.news_id)
        )
        if inserted.scalar_one_or_none() is None:
            return
        await self._session.execute(
            delete(NewsEventRegionRow).where(NewsEventRegionRow.news_id == event.news_id)
        )
        await self._session.execute(
            delete(NewsEventEntityRow).where(NewsEventEntityRow.news_id == event.news_id)
        )
        await self._session.execute(
            delete(NewsEventTopicRow).where(NewsEventTopicRow.news_id == event.news_id)
        )
        self._session.add_all(
            [
                NewsEventRegionRow(news_id=event.news_id, region=region.value)
                for region in event.regions
            ]
        )
        self._session.add_all(
            [
                NewsEventEntityRow(
                    news_id=event.news_id,
                    entity_id=entity.entity_id,
                    entity_type=entity.entity_type,
                )
                for entity in event.entities
            ]
        )
        self._session.add_all(
            [NewsEventTopicRow(news_id=event.news_id, topic=topic) for topic in event.topics]
        )


class PostgresDataRepository:
    """Production read repository; all point-in-time filters execute in PostgreSQL."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_instruments(self, query: InstrumentQuery) -> list[Instrument]:
        statement = select(InstrumentRow.payload).where(
            InstrumentRow.region.in_([region.value for region in query.regions])
        )
        if query.venues:
            statement = statement.where(
                InstrumentRow.payload["venue_mic"].as_string().in_(sorted(query.venues))
            )
        if query.asset_classes:
            statement = statement.where(
                InstrumentRow.payload["asset_class"]
                .as_string()
                .in_(sorted(asset_class.value for asset_class in query.asset_classes))
            )
        if query.active_on is not None:
            statement = statement.where(
                InstrumentRow.valid_from <= query.active_on,
                or_(InstrumentRow.valid_to.is_(None), InstrumentRow.valid_to > query.active_on),
            )
        if query.modified_since is not None:
            statement = statement.where(InstrumentRow.updated_at >= query.modified_since)
        statement = statement.order_by(InstrumentRow.instrument_id).limit(query.limit)
        async with self._database.session() as session:
            payloads = (await session.scalars(statement)).all()
        return [Instrument.model_validate(payload) for payload in payloads]

    async def list_bars(self, query: BarQuery) -> list[MarketBar]:
        versions = self._current_market_bar_versions(query.as_of)
        statement = (
            select(versions.c.payload)
            .where(
                versions.c.instrument_id.in_(query.instrument_ids),
                versions.c.interval == query.interval.value,
                versions.c.adjustment == query.adjustment.value,
                versions.c.bar_start >= query.start,
                versions.c.bar_start < query.end,
            )
            .order_by(versions.c.instrument_id, versions.c.bar_start, versions.c.bar_id)
            .limit(query.limit)
        )
        async with self._database.session() as session:
            payloads = (await session.scalars(statement)).all()
        return [MarketBar.model_validate(payload) for payload in payloads]

    async def list_snapshots(self, query: MarketSnapshotQuery) -> list[MarketSnapshot]:
        versions = self._current_market_bar_versions(query.as_of)
        rank = func.row_number().over(
            partition_by=versions.c.instrument_id,
            order_by=(versions.c.bar_end.desc(), versions.c.available_at.desc()),
        )
        latest_bars = (
            select(
                versions.c.instrument_id.label("instrument_id"),
                versions.c.payload.label("payload"),
                rank.label("rank"),
            )
            .where(
                versions.c.instrument_id.in_(query.instrument_ids),
                versions.c.interval == Interval.D1.value,
                versions.c.adjustment == Adjustment.RAW.value,
            )
            .subquery()
        )
        statement = (
            select(latest_bars.c.payload)
            .where(latest_bars.c.rank == 1)
            .order_by(latest_bars.c.instrument_id)
        )
        async with self._database.session() as session:
            payloads = (await session.scalars(statement)).all()
        bars = [MarketBar.model_validate(payload) for payload in payloads]
        return [
            MarketSnapshot(
                instrument_id=bar.instrument_id,
                canonical_symbol=bar.canonical_symbol,
                region=bar.region,
                price_time=bar.bar_end,
                last=bar.close,
                currency=bar.currency,
                available_at=bar.available_at,
                source_records=[bar.source],
            )
            for bar in bars
        ]

    @staticmethod
    def _current_market_bar_versions(as_of: datetime) -> Subquery:
        base_versions = select(
            MarketBarRow.bar_id.label("bar_id"),
            MarketBarRow.instrument_id.label("instrument_id"),
            MarketBarRow.interval.label("interval"),
            MarketBarRow.adjustment.label("adjustment"),
            MarketBarRow.bar_start.label("bar_start"),
            MarketBarRow.bar_end.label("bar_end"),
            MarketBarRow.available_at.label("available_at"),
            literal(0).label("revision_rank"),
            MarketBarRow.payload["source"]["checksum_sha256"].as_string().label("source_checksum"),
            MarketBarRow.payload.label("payload"),
        ).where(MarketBarRow.available_at <= as_of)
        revision_versions = (
            select(
                MarketBarRow.bar_id.label("bar_id"),
                MarketBarRow.instrument_id.label("instrument_id"),
                MarketBarRow.interval.label("interval"),
                MarketBarRow.adjustment.label("adjustment"),
                MarketBarRow.bar_start.label("bar_start"),
                MarketBarRow.bar_end.label("bar_end"),
                MarketBarRevisionRow.available_at.label("available_at"),
                literal(1).label("revision_rank"),
                MarketBarRevisionRow.source_checksum_sha256.label("source_checksum"),
                MarketBarRevisionRow.payload.label("payload"),
            )
            .join(MarketBarRow, MarketBarRow.bar_id == MarketBarRevisionRow.bar_id)
            .where(MarketBarRevisionRow.available_at <= as_of)
        )
        versions = union_all(base_versions, revision_versions).subquery()
        rank = func.row_number().over(
            partition_by=versions.c.bar_id,
            order_by=(
                versions.c.available_at.desc(),
                versions.c.revision_rank.desc(),
                versions.c.source_checksum.desc(),
            ),
        )
        ranked = select(
            versions.c.bar_id,
            versions.c.instrument_id,
            versions.c.interval,
            versions.c.adjustment,
            versions.c.bar_start,
            versions.c.bar_end,
            versions.c.available_at,
            versions.c.payload,
            rank.label("rank"),
        ).subquery()
        return (
            select(
                ranked.c.bar_id,
                ranked.c.instrument_id,
                ranked.c.interval,
                ranked.c.adjustment,
                ranked.c.bar_start,
                ranked.c.bar_end,
                ranked.c.available_at,
                ranked.c.payload,
            )
            .where(ranked.c.rank == 1)
            .subquery()
        )

    async def list_market_observations(
        self, query: MarketObservationQuery
    ) -> list[MarketObservation]:
        conditions = [
            MarketObservationRow.region.in_([region.value for region in query.regions]),
            MarketObservationRow.metric_code.in_(query.metric_codes),
            MarketObservationRow.observed_at >= query.start,
            MarketObservationRow.observed_at < query.end,
            MarketObservationRow.available_at <= query.as_of,
        ]
        if query.scope_ids:
            conditions.append(MarketObservationRow.scope_id.in_(query.scope_ids))
        statement = (
            select(MarketObservationRow.payload)
            .where(*conditions)
            .order_by(
                MarketObservationRow.observed_at,
                MarketObservationRow.metric_code,
                MarketObservationRow.observation_id,
            )
            .limit(query.limit)
        )
        async with self._database.session() as session:
            payloads = (await session.scalars(statement)).all()
        return [MarketObservation.model_validate(payload) for payload in payloads]

    async def list_macro_series(self, query: MacroSeriesQuery) -> list[MacroSeries]:
        statement = select(MacroSeriesRow.payload).where(
            MacroSeriesRow.region.in_([region.value for region in query.regions])
        )
        if query.series_ids:
            statement = statement.where(MacroSeriesRow.series_id.in_(query.series_ids))
        statement = statement.order_by(MacroSeriesRow.series_id).limit(query.limit)
        async with self._database.session() as session:
            payloads = (await session.scalars(statement)).all()
        return [MacroSeries.model_validate(payload) for payload in payloads]

    async def list_macro_observations(self, query: MacroObservationQuery) -> list[MacroObservation]:
        conditions = [
            MacroObservationRow.series_id.in_(query.series_ids),
            MacroObservationRow.period_end >= query.period_from,
            MacroObservationRow.period_end <= query.period_to,
            MacroObservationRow.available_at <= query.as_of,
        ]
        if query.revision_policy.value == "all_vintages":
            statement = (
                select(MacroObservationRow.payload)
                .where(*conditions)
                .order_by(
                    MacroObservationRow.series_id,
                    MacroObservationRow.period_end,
                    MacroObservationRow.available_at,
                    MacroObservationRow.revision_no,
                )
                .limit(query.limit)
            )
        else:
            descending = query.revision_policy.value == "latest_as_of"
            rank = func.row_number().over(
                partition_by=(MacroObservationRow.series_id, MacroObservationRow.period_end),
                order_by=(
                    MacroObservationRow.available_at.desc()
                    if descending
                    else MacroObservationRow.available_at.asc(),
                    MacroObservationRow.revision_no.desc()
                    if descending
                    else MacroObservationRow.revision_no.asc(),
                    MacroObservationRow.observation_id.desc()
                    if descending
                    else MacroObservationRow.observation_id.asc(),
                ),
            )
            ranked = (
                select(
                    MacroObservationRow.payload.label("payload"),
                    MacroObservationRow.series_id.label("series_id"),
                    MacroObservationRow.period_end.label("period_end"),
                    rank.label("rank"),
                )
                .where(*conditions)
                .subquery()
            )
            statement = (
                select(ranked.c.payload)
                .where(ranked.c.rank == 1)
                .order_by(ranked.c.series_id, ranked.c.period_end)
                .limit(query.limit)
            )
        async with self._database.session() as session:
            payloads = (await session.scalars(statement)).all()
        return [MacroObservation.model_validate(payload) for payload in payloads]

    async def list_macro_releases(self, query: MacroReleaseQuery) -> list[MacroRelease]:
        date_from, date_to = _date_only_window(query.scheduled_from, query.scheduled_to)
        base_conditions = (
            MacroReleaseRow.region.in_([region.value for region in query.regions]),
            or_(
                (MacroReleaseRow.scheduled_at >= query.scheduled_from)
                & (MacroReleaseRow.scheduled_at < query.scheduled_to),
                (MacroReleaseRow.scheduled_date >= date_from)
                & (MacroReleaseRow.scheduled_date <= date_to),
            ),
        )
        base_versions = select(
            MacroReleaseRow.release_id.label("release_id"),
            MacroReleaseRow.scheduled_at.label("scheduled_at"),
            MacroReleaseRow.scheduled_date.label("scheduled_date"),
            MacroReleaseRow.available_at.label("available_at"),
            literal(0).label("revision_rank"),
            func.coalesce(MacroReleaseRow.source_checksum_sha256, "").label("source_checksum"),
            MacroReleaseRow.payload.label("payload"),
        ).where(*base_conditions, MacroReleaseRow.available_at <= query.as_of)
        revision_versions = (
            select(
                MacroReleaseRow.release_id.label("release_id"),
                MacroReleaseRow.scheduled_at.label("scheduled_at"),
                MacroReleaseRow.scheduled_date.label("scheduled_date"),
                MacroReleaseRevisionRow.available_at.label("available_at"),
                literal(1).label("revision_rank"),
                MacroReleaseRevisionRow.source_checksum_sha256.label("source_checksum"),
                MacroReleaseRevisionRow.payload.label("payload"),
            )
            .join(MacroReleaseRow, MacroReleaseRow.release_id == MacroReleaseRevisionRow.release_id)
            .where(*base_conditions, MacroReleaseRevisionRow.available_at <= query.as_of)
        )
        versions = union_all(base_versions, revision_versions).subquery()
        rank = func.row_number().over(
            partition_by=versions.c.release_id,
            order_by=(
                versions.c.available_at.desc(),
                versions.c.revision_rank.desc(),
                versions.c.source_checksum.desc(),
            ),
        )
        ranked = select(
            versions.c.payload.label("payload"),
            versions.c.scheduled_at.label("scheduled_at"),
            versions.c.scheduled_date.label("scheduled_date"),
            versions.c.release_id.label("release_id"),
            rank.label("rank"),
        ).subquery()
        statement = (
            select(ranked.c.payload)
            .where(ranked.c.rank == 1)
            .order_by(
                func.coalesce(
                    ranked.c.scheduled_at,
                    cast(ranked.c.scheduled_date, DateTime(timezone=True)),
                ),
                ranked.c.release_id,
            )
            .limit(query.limit)
        )
        async with self._database.session() as session:
            payloads = (await session.scalars(statement)).all()
        return [MacroRelease.model_validate(payload) for payload in payloads]

    async def list_news(self, query: NewsQuery) -> list[NewsEvent]:
        date_from, date_to = _date_only_window(query.published_from, query.published_to)
        published_sort_at = func.coalesce(
            NewsEventRow.published_at,
            cast(NewsEventRow.published_date, DateTime(timezone=True)),
        )
        statement = (
            select(
                NewsEventRow.payload,
                NewsEventRow.published_at,
                NewsEventRow.news_id,
                published_sort_at.label("published_sort_at"),
            )
            .join(NewsEventRegionRow)
            .where(
                NewsEventRegionRow.region.in_([region.value for region in query.regions]),
                or_(
                    (NewsEventRow.published_at >= query.published_from)
                    & (NewsEventRow.published_at < query.published_to),
                    (NewsEventRow.published_date >= date_from)
                    & (NewsEventRow.published_date <= date_to),
                ),
                NewsEventRow.available_at <= query.as_of,
            )
        )
        if query.entity_ids:
            statement = statement.join(NewsEventEntityRow).where(
                NewsEventEntityRow.entity_id.in_(query.entity_ids)
            )
        if query.topics:
            statement = statement.join(NewsEventTopicRow).where(
                NewsEventTopicRow.topic.in_(query.topics)
            )
        if query.languages:
            statement = statement.where(
                NewsEventRow.payload["language"].as_string().in_(sorted(query.languages))
            )
        if query.source_tiers:
            statement = statement.where(
                NewsEventRow.payload["source_tier"]
                .as_string()
                .in_(sorted(source_tier.value for source_tier in query.source_tiers))
            )
        statement = (
            statement.distinct()
            .order_by(
                published_sort_at.desc(),
                NewsEventRow.news_id,
            )
            .limit(query.limit)
        )
        async with self._database.session() as session:
            payloads = (await session.scalars(statement)).all()
        events = [NewsEvent.model_validate(payload) for payload in payloads]
        if not query.include_superseded:
            superseded = {
                event.supersedes_news_id for event in events if event.supersedes_news_id is not None
            }
            events = [event for event in events if event.news_id not in superseded]
        return [self._limit_news_content(event, query) for event in events]

    @staticmethod
    def _limit_news_content(event: NewsEvent, query: NewsQuery) -> NewsEvent:
        if query.content_mode is ContentMode.HEADLINE:
            return event.model_copy(
                update={"summary": None, "body": None, "content_mode": ContentMode.HEADLINE}
            )
        if query.content_mode is ContentMode.SNIPPET:
            return event.model_copy(update={"body": None, "content_mode": ContentMode.SNIPPET})
        return event


def _date_only_window(start: datetime, end: datetime) -> tuple[date, date]:
    """Return the inclusive date-only records that overlap ``[start, end)``."""

    return (
        start.astimezone(UTC).date(),
        (end.astimezone(UTC) - timedelta(microseconds=1)).date(),
    )


@dataclass(frozen=True)
class IngestionRunLease:
    run_id: UUID
    attempt_no: int


@dataclass(frozen=True)
class ScheduledTaskCheckpoint:
    report_date: date
    task_id: str
    provider_role: str
    dataset: Dataset
    region: str
    request_as_of: datetime
    status: Literal["active", "completed"]
    next_cursor: str | None
    source_watermark: str | None
    run_id: UUID | None
    run_ids: tuple[UUID, ...]
    records_accepted: int
    records_rejected: int
    lease_epoch: int
    lease_owner_id: UUID | None


class ScheduledTaskCheckpointRepository:
    """Persist scheduler progress independently from page-level fact commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def begin_or_load(
        self,
        *,
        report_date: date,
        task_id: str,
        provider_role: str,
        dataset: Dataset,
        region: str,
        request_as_of: datetime,
        lease_owner_id: UUID,
    ) -> ScheduledTaskCheckpoint:
        claimed = await self._session.execute(
            insert(ScheduledTaskCheckpointRow)
            .values(
                report_date=report_date,
                task_id=task_id,
                provider_role=provider_role,
                dataset=dataset.value,
                region=region,
                request_as_of=request_as_of,
                status="active",
                records_accepted=0,
                records_rejected=0,
                lease_epoch=1,
                lease_owner_id=lease_owner_id,
            )
            .on_conflict_do_update(
                index_elements=(
                    ScheduledTaskCheckpointRow.report_date,
                    ScheduledTaskCheckpointRow.task_id,
                ),
                set_={
                    "lease_epoch": ScheduledTaskCheckpointRow.lease_epoch + 1,
                    "lease_owner_id": lease_owner_id,
                },
                where=ScheduledTaskCheckpointRow.status == "active",
            )
            .returning(ScheduledTaskCheckpointRow)
        )
        row = claimed.scalar_one_or_none()
        if row is None:
            row = await self._session.get(ScheduledTaskCheckpointRow, (report_date, task_id))
        if row is None:
            raise RuntimeError("scheduled task checkpoint was not persisted")
        if (
            row.provider_role != provider_role
            or row.dataset != dataset.value
            or row.region != region
        ):
            raise ValueError("scheduled task checkpoint identity was reused for another task")
        return _scheduled_task_checkpoint(row)

    async def advance(
        self,
        checkpoint: ScheduledTaskCheckpoint,
        *,
        run_id: UUID,
        next_cursor: str | None,
        source_watermark: str | None,
        records_accepted: int,
        records_rejected: int,
    ) -> ScheduledTaskCheckpoint:
        if checkpoint.status == "completed":
            raise ValueError("completed scheduled task checkpoint cannot advance")
        completed = next_cursor is None
        run_ids = (
            checkpoint.run_ids if run_id in checkpoint.run_ids else (*checkpoint.run_ids, run_id)
        )
        updated = await self._session.execute(
            update(ScheduledTaskCheckpointRow)
            .where(
                ScheduledTaskCheckpointRow.report_date == checkpoint.report_date,
                ScheduledTaskCheckpointRow.task_id == checkpoint.task_id,
                ScheduledTaskCheckpointRow.status == "active",
                ScheduledTaskCheckpointRow.lease_epoch == checkpoint.lease_epoch,
                ScheduledTaskCheckpointRow.lease_owner_id == checkpoint.lease_owner_id,
            )
            .values(
                status="completed" if completed else "active",
                run_id=run_id,
                run_ids=[str(value) for value in run_ids],
                next_cursor=next_cursor,
                source_watermark=source_watermark,
                records_accepted=ScheduledTaskCheckpointRow.records_accepted + records_accepted,
                records_rejected=ScheduledTaskCheckpointRow.records_rejected + records_rejected,
            )
            .returning(ScheduledTaskCheckpointRow)
        )
        row = updated.scalar_one_or_none()
        if row is None:
            raise RuntimeError("scheduled task checkpoint was lost before it advanced")
        return _scheduled_task_checkpoint(row)


def _scheduled_task_checkpoint(row: ScheduledTaskCheckpointRow) -> ScheduledTaskCheckpoint:
    if row.status not in {"active", "completed"}:
        raise RuntimeError("scheduled task checkpoint has an invalid status")
    try:
        run_ids = tuple(UUID(value) for value in row.run_ids)
    except (TypeError, ValueError) as error:
        raise RuntimeError("scheduled task checkpoint has invalid run IDs") from error
    if len(run_ids) != len(set(run_ids)):
        raise RuntimeError("scheduled task checkpoint has duplicate run IDs")
    return ScheduledTaskCheckpoint(
        report_date=row.report_date,
        task_id=row.task_id,
        provider_role=row.provider_role,
        dataset=Dataset(row.dataset),
        region=row.region,
        request_as_of=row.request_as_of,
        status=typing_cast(Literal["active", "completed"], row.status),
        next_cursor=row.next_cursor,
        source_watermark=row.source_watermark,
        run_id=row.run_id,
        run_ids=run_ids,
        records_accepted=row.records_accepted,
        records_rejected=row.records_rejected,
        lease_epoch=row.lease_epoch,
        lease_owner_id=row.lease_owner_id,
    )


class IngestionRunRepository:
    """Durable ingestion-run idempotency separate from worker orchestration."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def acquire_run(
        self,
        request: IngestJobRequest,
        *,
        idempotency_key: str,
        run_id: UUID,
        now: datetime,
        lease_expires_at: datetime,
    ) -> tuple[IngestionRunLease | None, UUID]:
        request_payload = request.model_dump(mode="json")
        inserted = await self._session.execute(
            insert(ProviderRunRow)
            .values(
                run_id=run_id,
                idempotency_key=idempotency_key,
                provider_role=request.provider_role,
                dataset=request.dataset.value,
                status="running",
                attempt_no=1,
                lease_expires_at=lease_expires_at,
                started_at=now,
                request_payload=request_payload,
            )
            .on_conflict_do_nothing(index_elements=(ProviderRunRow.idempotency_key,))
            .returning(ProviderRunRow.run_id, ProviderRunRow.attempt_no)
        )
        reserved = inserted.one_or_none()
        if reserved is not None:
            return IngestionRunLease(run_id=reserved.run_id, attempt_no=reserved.attempt_no), run_id
        existing = await self._session.scalar(
            select(ProviderRunRow).where(ProviderRunRow.idempotency_key == idempotency_key)
        )
        if existing is None:
            raise RuntimeError("ingestion-run idempotency conflict did not return an existing run")
        if existing.request_payload != request_payload:
            raise ValueError("ingestion-run idempotency key was reused for a different request")
        return None, existing.run_id

    async def claim_recoverable_run(
        self,
        run_id: UUID,
        *,
        now: datetime,
        lease_expires_at: datetime,
    ) -> IngestionRunLease | None:
        claimed = await self._session.execute(
            update(ProviderRunRow)
            .where(
                ProviderRunRow.run_id == run_id,
                or_(
                    ProviderRunRow.status.in_(("failed", "retry_wait")),
                    (
                        (ProviderRunRow.status == "running")
                        & (
                            (ProviderRunRow.lease_expires_at.is_(None))
                            | (ProviderRunRow.lease_expires_at <= now)
                        )
                    ),
                ),
            )
            .values(
                status="running",
                attempt_no=ProviderRunRow.attempt_no + 1,
                lease_expires_at=lease_expires_at,
                started_at=now,
                finished_at=None,
                error_code=None,
            )
            .returning(ProviderRunRow.run_id, ProviderRunRow.attempt_no)
        )
        row = claimed.one_or_none()
        return (
            None if row is None else IngestionRunLease(run_id=row.run_id, attempt_no=row.attempt_no)
        )

    async def renew_lease(self, lease: IngestionRunLease, *, lease_expires_at: datetime) -> bool:
        renewed = await self._session.execute(
            update(ProviderRunRow)
            .where(
                ProviderRunRow.run_id == lease.run_id,
                ProviderRunRow.attempt_no == lease.attempt_no,
                ProviderRunRow.status == "running",
            )
            .values(lease_expires_at=lease_expires_at)
            .returning(ProviderRunRow.run_id)
        )
        return renewed.scalar_one_or_none() is not None

    async def complete_run(self, result: IngestJobResult, *, lease: IngestionRunLease) -> bool:
        completed = await self._session.execute(
            update(ProviderRunRow)
            .where(
                ProviderRunRow.run_id == result.run_id,
                ProviderRunRow.attempt_no == lease.attempt_no,
                ProviderRunRow.status == "running",
            )
            .values(
                status=result.status,
                started_at=result.started_at,
                finished_at=result.finished_at,
                records_fetched=result.records_fetched,
                records_accepted=result.records_accepted,
                records_rejected=result.records_rejected,
                error_code=result.error_code,
                lease_expires_at=None,
                details={
                    "records_inserted": result.records_inserted,
                    "records_updated": result.records_updated,
                    "next_cursor": result.next_cursor,
                    "source_watermark": result.source_watermark,
                    "retry_after_seconds": result.retry_after_seconds,
                    "warnings": [warning.model_dump(mode="json") for warning in result.warnings],
                },
            )
            .returning(ProviderRunRow.run_id)
        )
        return completed.scalar_one_or_none() is not None

    async def fail_run(self, lease: IngestionRunLease, *, error_code: str) -> bool:
        failed = await self._session.execute(
            update(ProviderRunRow)
            .where(
                ProviderRunRow.run_id == lease.run_id,
                ProviderRunRow.attempt_no == lease.attempt_no,
                ProviderRunRow.status == "running",
            )
            .values(status="failed", error_code=error_code, lease_expires_at=None)
            .returning(ProviderRunRow.run_id)
        )
        return failed.scalar_one_or_none() is not None

    async def load_run(self, run_id: UUID) -> ProviderRunRow | None:
        return await self._session.get(ProviderRunRow, run_id)

    async def load_completed_result(self, run_id: UUID) -> IngestJobResult | None:
        row = await self.load_run(run_id)
        if row is None or row.status not in {"succeeded", "partial"} or row.finished_at is None:
            return None
        details = row.details
        return IngestJobResult(
            run_id=row.run_id,
            status=row.status,
            provider_role=row.provider_role,
            dataset=Dataset(row.dataset),
            started_at=row.started_at,
            finished_at=row.finished_at,
            records_fetched=row.records_fetched,
            records_accepted=row.records_accepted,
            records_rejected=row.records_rejected,
            records_inserted=details.get("records_inserted", 0),
            records_updated=details.get("records_updated", 0),
            next_cursor=details.get("next_cursor"),
            source_watermark=details.get("source_watermark"),
            error_code=row.error_code,
            retry_after_seconds=details.get("retry_after_seconds"),
            warnings=[WarningItem.model_validate(item) for item in details.get("warnings", [])],
        )


class ReportRepository:
    """Immutable report/snapshot storage and idempotent delivery recovery seam."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def put_generation_attempt(self, attempt: ReportGenerationAttempt) -> bool:
        inserted = await self._session.execute(
            insert(ReportGenerationAttemptRow)
            .values(
                generation_id=attempt.generation_id,
                report_id=attempt.report_id,
                report_version=attempt.report_version,
                input_snapshot_id=attempt.input_snapshot_id,
                lifecycle_status=attempt.lifecycle_status,
                attempt_no=attempt.attempt_no,
                prompt_version=attempt.prompt_version,
                model=attempt.model,
                model_parameters=attempt.model_parameters,
                input_fingerprint_sha256=attempt.input_fingerprint_sha256,
                source_ref_ids=attempt.source_ref_ids,
                error_code=attempt.error_code,
                response_payload=attempt.response_payload,
            )
            .on_conflict_do_nothing()
            .returning(ReportGenerationAttemptRow.generation_id)
        )
        if inserted.scalar_one_or_none() is not None:
            return True
        existing = await self.load_generation_attempt(attempt.generation_id)
        if existing is None:
            raise ValueError("generation attempt identity is already owned by another attempt")
        if existing != attempt:
            raise ValueError("generation attempt is immutable")
        return False

    async def update_generation_attempt(self, attempt: ReportGenerationAttempt) -> None:
        updated = await self._session.execute(
            update(ReportGenerationAttemptRow)
            .where(ReportGenerationAttemptRow.generation_id == attempt.generation_id)
            .values(
                lifecycle_status=attempt.lifecycle_status,
                attempt_no=attempt.attempt_no,
                error_code=attempt.error_code,
                response_payload=attempt.response_payload,
            )
            .returning(ReportGenerationAttemptRow.generation_id)
        )
        if updated.scalar_one_or_none() is None:
            raise ValueError("generation attempt does not exist")

    async def load_generation_attempt(self, generation_id: UUID) -> ReportGenerationAttempt | None:
        row = await self._session.get(ReportGenerationAttemptRow, generation_id)
        if row is None:
            return None
        return ReportGenerationAttempt(
            generation_id=row.generation_id,
            report_id=row.report_id,
            report_version=row.report_version,
            input_snapshot_id=row.input_snapshot_id,
            lifecycle_status=row.lifecycle_status,
            attempt_no=row.attempt_no,
            prompt_version=row.prompt_version,
            model=row.model,
            model_parameters=row.model_parameters,
            input_fingerprint_sha256=row.input_fingerprint_sha256,
            source_ref_ids=row.source_ref_ids,
            error_code=row.error_code,
            response_payload=row.response_payload,
        )

    async def put_input_snapshot(self, snapshot: ReportInputSnapshot) -> bool:
        inserted = await self._session.execute(
            insert(ReportInputSnapshotRow)
            .values(
                snapshot_id=snapshot.snapshot_id,
                snapshot_version=snapshot.snapshot_version,
                report_date=snapshot.report_date,
                as_of=snapshot.as_of,
                cutoff_at=snapshot.cutoff_at,
                fingerprint_sha256=snapshot.fingerprint_sha256,
                fact_ids=snapshot.fact_ids,
                payload=snapshot.payload,
            )
            .on_conflict_do_nothing()
            .returning(ReportInputSnapshotRow.snapshot_id)
        )
        if inserted.scalar_one_or_none() is not None:
            return True
        existing = await self._session.get(ReportInputSnapshotRow, snapshot.snapshot_id)
        if existing is None:
            raise ValueError(
                "report input snapshot identity is already owned by another snapshot ID"
            )
        if (
            existing.snapshot_version != snapshot.snapshot_version
            or existing.report_date != snapshot.report_date
            or existing.as_of != snapshot.as_of
            or existing.cutoff_at != snapshot.cutoff_at
            or existing.fingerprint_sha256 != snapshot.fingerprint_sha256
            or existing.fact_ids != snapshot.fact_ids
            or existing.payload != snapshot.payload
        ):
            raise ValueError("report input snapshot is immutable")
        return False

    async def put_report(self, report: StoredDailyReport) -> bool:
        snapshot = await self.load_input_snapshot(report.input_snapshot_id)
        if snapshot is None:
            raise ValueError("daily report references an unknown input snapshot")
        payload_snapshot = report.payload.get("input_snapshot")
        expected_payload_snapshot = {
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_version": snapshot.snapshot_version,
            "as_of": snapshot.as_of.isoformat().replace("+00:00", "Z"),
            "cutoff_at": snapshot.cutoff_at.isoformat().replace("+00:00", "Z"),
            "fingerprint_sha256": snapshot.fingerprint_sha256,
            "fact_ids": snapshot.fact_ids,
        }
        if payload_snapshot != expected_payload_snapshot:
            raise ValueError("daily report payload input_snapshot must match stored snapshot")
        report.validate_fact_references(snapshot.fact_ids)
        inserted = await self._session.execute(
            insert(DailyReportRow)
            .values(
                report_id=report.report_id,
                report_date=report.report_date,
                report_version=report.report_version,
                contract_version=report.contract_version,
                input_snapshot_id=report.input_snapshot_id,
                status=report.status,
                publication_decision=report.publication_decision,
                lifecycle_status=report.lifecycle_status,
                generated_at=report.generated_at,
                generation_id=report.generation_id,
                validation_errors=[
                    issue.model_dump(mode="json") for issue in report.validation_errors
                ],
                payload=report.payload,
            )
            .on_conflict_do_nothing()
            .returning(DailyReportRow.report_id)
        )
        if inserted.scalar_one_or_none() is not None:
            source_references = report.source_references()
            if source_references:
                await self._session.execute(
                    insert(DailyReportSourceRefRow).values(
                        [
                            {
                                "report_id": report.report_id,
                                "source_ref_id": source.source_ref_id,
                                "provider_id": source.provider_id,
                                "provider_record_id": source.provider_record_id,
                                "checksum_sha256": source.checksum_sha256,
                                "retrieved_at": source.retrieved_at,
                            }
                            for source in source_references
                        ]
                    )
                )
            return True
        existing = await self._session.get(DailyReportRow, report.report_id)
        if existing is None:
            raise ValueError("report date/version is already owned by another report ID")
        if (
            existing.report_date != report.report_date
            or existing.report_version != report.report_version
            or existing.contract_version != report.contract_version
            or existing.input_snapshot_id != report.input_snapshot_id
            or existing.status != report.status
            or existing.publication_decision != report.publication_decision
            or existing.lifecycle_status != report.lifecycle_status
            or existing.generated_at != report.generated_at
            or getattr(existing, "generation_id", None) != report.generation_id
            or getattr(existing, "validation_errors", [])
            != [issue.model_dump(mode="json") for issue in report.validation_errors]
            or existing.payload != report.payload
        ):
            raise ValueError("daily report is immutable")
        return False

    async def update_report_validation(
        self,
        report: StoredDailyReport,
        *,
        expected_lifecycle_status: str,
    ) -> bool:
        """Atomically advance report validation state without overwriting another worker."""

        updated = await self._session.execute(
            update(DailyReportRow)
            .where(
                DailyReportRow.report_id == report.report_id,
                DailyReportRow.lifecycle_status == expected_lifecycle_status,
            )
            .values(
                status=report.status,
                publication_decision=report.publication_decision,
                lifecycle_status=report.lifecycle_status,
                validation_errors=[
                    issue.model_dump(mode="json") for issue in report.validation_errors
                ],
                payload=report.payload,
            )
            .returning(DailyReportRow.report_id)
        )
        return updated.scalar_one_or_none() is not None

    async def reserve_delivery_attempt(self, attempt: DeliveryAttempt) -> bool:
        inserted = await self._session.execute(
            insert(DeliveryAttemptRow)
            .values(
                delivery_id=attempt.delivery_id,
                report_id=attempt.report_id,
                delivery_target=attempt.delivery_target,
                idempotency_key=attempt.idempotency_key,
                attempt_no=attempt.attempt_no,
                status=attempt.status,
                request_payload=attempt.request_payload,
                response_payload=attempt.response_payload,
            )
            .on_conflict_do_nothing(
                constraint="uq_delivery_attempt_idempotency",
            )
            .returning(DeliveryAttemptRow.delivery_id)
        )
        if inserted.scalar_one_or_none() is not None:
            return True
        existing = await self.load_delivery_attempt_for_key(
            report_id=attempt.report_id,
            delivery_target=attempt.delivery_target,
            idempotency_key=attempt.idempotency_key,
        )
        if existing is None:
            raise RuntimeError("delivery idempotency conflict did not return an existing attempt")
        if existing.request_payload != attempt.request_payload:
            raise ValueError("delivery idempotency key was reused for a different request")
        return False

    async def update_delivery_attempt(
        self,
        *,
        delivery_id: UUID,
        expected_attempt_no: int,
        status: Literal["failed", "retry_wait", "succeeded"],
        response_payload: dict[str, Any] | None,
    ) -> bool:
        if expected_attempt_no < 1:
            raise ValueError("expected delivery attempt number must be positive")
        updated = await self._session.execute(
            update(DeliveryAttemptRow)
            .where(
                DeliveryAttemptRow.delivery_id == delivery_id,
                DeliveryAttemptRow.attempt_no == expected_attempt_no,
                DeliveryAttemptRow.status == "pending",
            )
            .values(
                status=status,
                response_payload=response_payload,
            )
            .returning(DeliveryAttemptRow.delivery_id)
        )
        return updated.scalar_one_or_none() is not None

    async def retry_delivery_attempt(self, delivery_id: UUID) -> bool:
        """Atomically resume a failed/pending delivery without inserting a duplicate."""

        resumed = await self._session.execute(
            update(DeliveryAttemptRow)
            .where(
                DeliveryAttemptRow.delivery_id == delivery_id,
                DeliveryAttemptRow.status.in_(("failed", "retry_wait")),
            )
            .values(
                status="pending",
                response_payload=None,
                attempt_no=DeliveryAttemptRow.attempt_no + 1,
            )
            .returning(DeliveryAttemptRow.delivery_id)
        )
        return resumed.scalar_one_or_none() is not None

    async def load_report(self, report_id: str) -> StoredDailyReport | None:
        row = await self._session.get(DailyReportRow, report_id)
        if row is None:
            return None
        return StoredDailyReport(
            report_id=row.report_id,
            report_date=row.report_date,
            report_version=row.report_version,
            contract_version=row.contract_version,
            input_snapshot_id=row.input_snapshot_id,
            status=row.status,
            publication_decision=row.publication_decision,
            generated_at=row.generated_at,
            payload=row.payload,
            lifecycle_status=row.lifecycle_status,
            generation_id=getattr(row, "generation_id", None),
            validation_errors=getattr(row, "validation_errors", []),
        )

    async def load_input_snapshot(self, snapshot_id: str) -> ReportInputSnapshot | None:
        row = await self._session.get(ReportInputSnapshotRow, snapshot_id)
        if row is None:
            return None
        return ReportInputSnapshot(
            snapshot_id=row.snapshot_id,
            snapshot_version=row.snapshot_version,
            report_date=row.report_date,
            as_of=row.as_of,
            cutoff_at=row.cutoff_at,
            fingerprint_sha256=row.fingerprint_sha256,
            fact_ids=row.fact_ids,
            payload=row.payload,
        )

    async def load_delivery_attempt(self, delivery_id: UUID) -> DeliveryAttempt | None:
        row = await self._session.get(DeliveryAttemptRow, delivery_id)
        if row is None:
            return None
        return DeliveryAttempt(
            delivery_id=row.delivery_id,
            report_id=row.report_id,
            delivery_target=row.delivery_target,
            idempotency_key=row.idempotency_key,
            attempt_no=row.attempt_no,
            status=row.status,
            request_payload=row.request_payload,
            response_payload=row.response_payload,
        )

    async def load_delivery_attempt_for_key(
        self,
        *,
        report_id: str,
        delivery_target: str,
        idempotency_key: str,
    ) -> DeliveryAttempt | None:
        row = await self._session.scalar(
            select(DeliveryAttemptRow).where(
                DeliveryAttemptRow.report_id == report_id,
                DeliveryAttemptRow.delivery_target == delivery_target,
                DeliveryAttemptRow.idempotency_key == idempotency_key,
            )
        )
        if row is None:
            return None
        return DeliveryAttempt(
            delivery_id=row.delivery_id,
            report_id=row.report_id,
            delivery_target=row.delivery_target,
            idempotency_key=row.idempotency_key,
            attempt_no=row.attempt_no,
            status=row.status,
            request_payload=row.request_payload,
            response_payload=row.response_payload,
        )


class IngestionCheckpointRepository(NormalizedFactRepository):
    """Database boundary for durable ingest audit and checkpoint records."""

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

    def add_rejection(
        self,
        *,
        run_id: UUID,
        provider_id: str,
        error_code: str,
        redacted_payload: dict[str, Any],
    ) -> None:
        self._session.add(
            IngestRejectionRow(
                run_id=run_id,
                provider_id=provider_id,
                error_code=error_code,
                redacted_payload=redacted_payload,
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
                ingestion_run_id=self._ingestion_run_id,
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
