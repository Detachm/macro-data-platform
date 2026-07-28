"""PIT SQL readers and report-fact projections for scheduled input evidence."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import case, func, literal, or_, select, tuple_, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Subquery

from macro_platform.contracts.common import Region
from macro_platform.contracts.macro import MacroRelease
from macro_platform.contracts.market import MarketBar
from macro_platform.contracts.news import NewsEvent
from macro_platform.normalization.common import stable_id
from macro_platform.storage.models import (
    MacroReleaseRevisionRow,
    MacroReleaseRow,
    MarketBarRevisionRow,
    MarketBarRow,
)


def isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _market_bar_versions(available_at: datetime, *, run_ids: tuple[UUID, ...]) -> Subquery:
    base_versions = select(
        MarketBarRow.bar_id.label("bar_id"),
        MarketBarRow.instrument_id.label("instrument_id"),
        MarketBarRow.region.label("region"),
        MarketBarRow.trading_date.label("trading_date"),
        MarketBarRow.available_at.label("available_at"),
        MarketBarRow.ingestion_run_id.label("ingestion_run_id"),
        MarketBarRow.provider_record_id.label("provider_record_id"),
        literal(0).label("revision_rank"),
        case((MarketBarRow.ingestion_run_id.in_(run_ids), 1), else_=0).label("evidence_rank"),
        MarketBarRow.payload["source"]["checksum_sha256"].as_string().label("source_checksum"),
        MarketBarRow.payload.label("payload"),
    ).where(MarketBarRow.available_at <= available_at)
    revision_versions = (
        select(
            MarketBarRow.bar_id.label("bar_id"),
            MarketBarRow.instrument_id.label("instrument_id"),
            MarketBarRow.region.label("region"),
            MarketBarRow.trading_date.label("trading_date"),
            MarketBarRevisionRow.available_at.label("available_at"),
            MarketBarRevisionRow.ingestion_run_id.label("ingestion_run_id"),
            MarketBarRevisionRow.payload["source"]["provider_record_id"]
            .as_string()
            .label("provider_record_id"),
            literal(1).label("revision_rank"),
            case((MarketBarRevisionRow.ingestion_run_id.in_(run_ids), 1), else_=0).label(
                "evidence_rank"
            ),
            MarketBarRevisionRow.source_checksum_sha256.label("source_checksum"),
            MarketBarRevisionRow.payload.label("payload"),
        )
        .join(MarketBarRow, MarketBarRow.bar_id == MarketBarRevisionRow.bar_id)
        .where(MarketBarRevisionRow.available_at <= available_at)
    )
    versions = union_all(base_versions, revision_versions).subquery()
    rank = func.row_number().over(
        partition_by=versions.c.bar_id,
        order_by=(
            versions.c.evidence_rank.desc(),
            versions.c.available_at.desc(),
            versions.c.revision_rank.desc(),
            versions.c.source_checksum.desc(),
        ),
    )
    ranked = select(
        versions.c.bar_id,
        versions.c.instrument_id,
        versions.c.region,
        versions.c.trading_date,
        versions.c.available_at,
        versions.c.ingestion_run_id,
        versions.c.provider_record_id,
        versions.c.revision_rank,
        versions.c.evidence_rank,
        versions.c.payload,
        rank.label("rank"),
    ).subquery()
    return (
        select(
            ranked.c.bar_id,
            ranked.c.instrument_id,
            ranked.c.region,
            ranked.c.trading_date,
            ranked.c.available_at,
            ranked.c.ingestion_run_id,
            ranked.c.provider_record_id,
            ranked.c.revision_rank,
            ranked.c.evidence_rank,
            ranked.c.payload,
        )
        .where(ranked.c.rank == 1)
        .subquery()
    )


async def market_bars_as_of(
    session: AsyncSession,
    *,
    region: Region,
    instrument_ids: frozenset[str],
    provider_record_ids: frozenset[str],
    run_ids: tuple[UUID, ...],
    available_at: datetime,
    report_date: date,
) -> tuple[MarketBar, ...]:
    if not provider_record_ids or not run_ids:
        return ()
    versions = _market_bar_versions(available_at, run_ids=run_ids)
    payloads = await session.scalars(
        select(versions.c.payload)
        .where(
            versions.c.region == region.value,
            versions.c.instrument_id.in_(sorted(instrument_ids)),
            versions.c.provider_record_id.in_(sorted(provider_record_ids)),
            versions.c.trading_date < report_date,
        )
        .order_by(
            versions.c.instrument_id,
            versions.c.trading_date.desc(),
            versions.c.available_at.desc(),
            versions.c.bar_id.desc(),
        )
    )
    return tuple(MarketBar.model_validate(payload) for payload in payloads.all())


async def late_market_instruments(
    session: AsyncSession,
    *,
    region: Region,
    provider_record_ids: frozenset[str],
    run_ids: tuple[UUID, ...],
    cutoff_at: datetime,
    as_of: datetime,
    expected_trading_date: date,
) -> set[str]:
    if not provider_record_ids or not run_ids:
        return set()
    versions = _market_bar_versions(as_of, run_ids=run_ids)
    rows = await session.scalars(
        select(versions.c.instrument_id).where(
            versions.c.region == region.value,
            versions.c.provider_record_id.in_(sorted(provider_record_ids)),
            versions.c.trading_date == expected_trading_date,
            versions.c.available_at > cutoff_at,
        )
    )
    return set(rows.all())


async def market_has_revision_before_cutoff(
    session: AsyncSession,
    *,
    selected_versions: set[tuple[str, str]],
    cutoff_at: datetime,
) -> bool:
    if not selected_versions:
        return False
    revision_id = await session.scalar(
        select(MarketBarRevisionRow.revision_id)
        .where(
            tuple_(
                MarketBarRevisionRow.bar_id,
                MarketBarRevisionRow.source_checksum_sha256,
            ).in_(sorted(selected_versions)),
            MarketBarRevisionRow.available_at <= cutoff_at,
        )
        .limit(1)
    )
    return revision_id is not None


def _macro_release_versions(available_at: datetime, *, run_ids: tuple[UUID, ...]) -> Subquery:
    base_versions = select(
        MacroReleaseRow.release_id.label("release_id"),
        MacroReleaseRow.region.label("region"),
        MacroReleaseRow.scheduled_at.label("scheduled_at"),
        MacroReleaseRow.scheduled_date.label("scheduled_date"),
        MacroReleaseRow.available_at.label("available_at"),
        MacroReleaseRow.ingestion_run_id.label("ingestion_run_id"),
        MacroReleaseRow.payload["source"]["provider_record_id"]
        .as_string()
        .label("provider_record_id"),
        literal(0).label("revision_rank"),
        case((MacroReleaseRow.ingestion_run_id.in_(run_ids), 1), else_=0).label("evidence_rank"),
        MacroReleaseRow.source_checksum_sha256.label("source_checksum"),
        MacroReleaseRow.payload.label("payload"),
    ).where(MacroReleaseRow.available_at <= available_at)
    revision_versions = (
        select(
            MacroReleaseRow.release_id.label("release_id"),
            MacroReleaseRow.region.label("region"),
            MacroReleaseRow.scheduled_at.label("scheduled_at"),
            MacroReleaseRow.scheduled_date.label("scheduled_date"),
            MacroReleaseRevisionRow.available_at.label("available_at"),
            MacroReleaseRevisionRow.ingestion_run_id.label("ingestion_run_id"),
            MacroReleaseRevisionRow.payload["source"]["provider_record_id"]
            .as_string()
            .label("provider_record_id"),
            literal(1).label("revision_rank"),
            case((MacroReleaseRevisionRow.ingestion_run_id.in_(run_ids), 1), else_=0).label(
                "evidence_rank"
            ),
            MacroReleaseRevisionRow.source_checksum_sha256.label("source_checksum"),
            MacroReleaseRevisionRow.payload.label("payload"),
        )
        .join(MacroReleaseRow, MacroReleaseRow.release_id == MacroReleaseRevisionRow.release_id)
        .where(MacroReleaseRevisionRow.available_at <= available_at)
    )
    versions = union_all(base_versions, revision_versions).subquery()
    rank = func.row_number().over(
        partition_by=versions.c.release_id,
        order_by=(
            versions.c.evidence_rank.desc(),
            versions.c.available_at.desc(),
            versions.c.revision_rank.desc(),
            versions.c.source_checksum.desc(),
        ),
    )
    ranked = select(
        versions.c.release_id,
        versions.c.region,
        versions.c.scheduled_at,
        versions.c.scheduled_date,
        versions.c.available_at,
        versions.c.ingestion_run_id,
        versions.c.provider_record_id,
        versions.c.revision_rank,
        versions.c.evidence_rank,
        versions.c.payload,
        rank.label("rank"),
    ).subquery()
    return (
        select(
            ranked.c.release_id,
            ranked.c.region,
            ranked.c.scheduled_at,
            ranked.c.scheduled_date,
            ranked.c.available_at,
            ranked.c.ingestion_run_id,
            ranked.c.provider_record_id,
            ranked.c.revision_rank,
            ranked.c.evidence_rank,
            ranked.c.payload,
        )
        .where(ranked.c.rank == 1)
        .subquery()
    )


def _calendar_window(
    versions: Subquery,
    *,
    report_date: date,
    cutoff_at: datetime,
) -> ColumnElement[bool]:
    end_date = report_date + timedelta(days=7)
    return or_(
        (versions.c.scheduled_at >= cutoff_at)
        & (versions.c.scheduled_at < cutoff_at + timedelta(days=7)),
        (versions.c.scheduled_date >= report_date) & (versions.c.scheduled_date < end_date),
    )


async def macro_releases_as_of(
    session: AsyncSession,
    *,
    region: Region,
    provider_record_ids: frozenset[str],
    run_ids: tuple[UUID, ...],
    report_date: date,
    cutoff_at: datetime,
) -> tuple[MacroRelease, ...]:
    if not provider_record_ids or not run_ids:
        return ()
    versions = _macro_release_versions(cutoff_at, run_ids=run_ids)
    payloads = await session.scalars(
        select(versions.c.payload)
        .where(
            versions.c.region == region.value,
            versions.c.provider_record_id.in_(sorted(provider_record_ids)),
            _calendar_window(versions, report_date=report_date, cutoff_at=cutoff_at),
        )
        .order_by(versions.c.scheduled_at, versions.c.scheduled_date, versions.c.release_id)
    )
    return tuple(MacroRelease.model_validate(payload) for payload in payloads.all())


async def has_late_macro_release(
    session: AsyncSession,
    *,
    region: Region,
    provider_record_ids: frozenset[str],
    run_ids: tuple[UUID, ...],
    report_date: date,
    cutoff_at: datetime,
    as_of: datetime,
) -> bool:
    if not provider_record_ids or not run_ids:
        return False
    versions = _macro_release_versions(as_of, run_ids=run_ids)
    release_id = await session.scalar(
        select(versions.c.release_id)
        .where(
            versions.c.region == region.value,
            versions.c.provider_record_id.in_(sorted(provider_record_ids)),
            versions.c.available_at > cutoff_at,
            _calendar_window(versions, report_date=report_date, cutoff_at=cutoff_at),
        )
        .limit(1)
    )
    return release_id is not None


async def macro_has_revision_before_cutoff(
    session: AsyncSession,
    *,
    selected_versions: set[tuple[str, str]],
    cutoff_at: datetime,
) -> bool:
    if not selected_versions:
        return False
    revision_id = await session.scalar(
        select(MacroReleaseRevisionRow.revision_id)
        .where(
            tuple_(
                MacroReleaseRevisionRow.release_id,
                MacroReleaseRevisionRow.source_checksum_sha256,
            ).in_(sorted(selected_versions)),
            MacroReleaseRevisionRow.available_at <= cutoff_at,
        )
        .limit(1)
    )
    return revision_id is not None


def market_bar_fact(
    bar: MarketBar,
    *,
    input_id: str,
    report_date: date,
    source_ref_id: str,
) -> dict[str, object]:
    return {
        "fact_id": stable_id("report-fact", input_id, bar.bar_id, bar.source.checksum_sha256),
        "input_id": input_id,
        "fact_type": "market_bar",
        "section_id": f"{bar.region.value.lower()}_highlights",
        "instrument_id": bar.instrument_id,
        "canonical_symbol": bar.canonical_symbol,
        "trading_date": bar.trading_date.isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": None if bar.volume is None else str(bar.volume),
        "currency": bar.currency,
        "label": f"{bar.canonical_symbol} close",
        "display_text": f"{bar.canonical_symbol} closed at {bar.close} {bar.currency}.",
        "value": str(bar.close),
        "unit": bar.currency,
        "available_at": isoformat(bar.available_at),
        "report_date": report_date.isoformat(),
        "source_ref_ids": [source_ref_id],
    }


def news_event_fact(
    event: NewsEvent,
    *,
    input_id: str,
    report_date: date,
    source_ref_id: str,
) -> dict[str, object]:
    published_at = None if event.published_at is None else isoformat(event.published_at)
    return {
        "fact_id": stable_id("report-fact", input_id, event.news_id, event.content_hash_sha256),
        "input_id": input_id,
        "fact_type": "news_event",
        "section_id": f"{event.regions[0].value.lower()}_highlights",
        "news_id": event.news_id,
        "title": event.title,
        "summary": event.summary,
        "published_at": published_at,
        "published_date": (
            None if event.published_date is None else event.published_date.isoformat()
        ),
        "content_mode": event.content_mode.value,
        "label": event.title,
        "display_text": event.title if event.summary is None else f"{event.title}: {event.summary}",
        "value": None,
        "available_at": isoformat(event.available_at),
        "report_date": report_date.isoformat(),
        "source_ref_ids": [source_ref_id],
    }


def macro_release_fact(
    release: MacroRelease,
    *,
    input_id: str,
    report_date: date,
    source_ref_id: str,
) -> dict[str, object]:
    return {
        "fact_id": stable_id(
            "report-fact", input_id, release.release_id, release.source.checksum_sha256
        ),
        "input_id": input_id,
        "fact_type": "macro_release",
        "section_id": "upcoming_calendar",
        "release_id": release.release_id,
        "series_id": release.series_id,
        "region": release.region.value,
        "release_name": release.release_name,
        "scheduled_at": None if release.scheduled_at is None else isoformat(release.scheduled_at),
        "scheduled_date": (
            None if release.scheduled_date is None else release.scheduled_date.isoformat()
        ),
        "status": release.status,
        "actual": None if release.actual is None else str(release.actual),
        "consensus": None if release.consensus is None else str(release.consensus),
        "previous": None if release.previous is None else str(release.previous),
        "label": release.release_name,
        "display_text": f"{release.release_name} is {release.status}.",
        "value": None if release.actual is None else str(release.actual),
        "unit": release.unit,
        "available_at": isoformat(release.available_at),
        "report_date": report_date.isoformat(),
        "source_ref_ids": [source_ref_id],
    }
