from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class InstrumentRow(Base):
    __tablename__ = "instruments"

    instrument_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    canonical_symbol: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    region: Mapped[str] = mapped_column(String(8), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    ingestion_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_runs.run_id", ondelete="RESTRICT"), nullable=True, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class InstrumentAliasRow(Base):
    __tablename__ = "instrument_aliases"
    __table_args__ = (
        UniqueConstraint(
            "provider_id", "source_symbol", "valid_from", name="uq_instrument_alias_effective"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_id: Mapped[str] = mapped_column(
        ForeignKey("instruments.instrument_id", ondelete="RESTRICT"), index=True
    )
    provider_id: Mapped[str] = mapped_column(String(64), index=True)
    source_symbol: Mapped[str] = mapped_column(String(128))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)


class MarketBarRow(Base):
    __tablename__ = "market_bars"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "interval",
            "bar_start",
            "adjustment",
            "provider_id",
            name="uq_market_bar_source",
        ),
        Index("ix_market_bars_instrument_end", "instrument_id", "bar_end"),
        Index("ix_market_bars_available", "available_at"),
    )

    bar_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(
        ForeignKey("instruments.instrument_id", ondelete="RESTRICT")
    )
    canonical_symbol: Mapped[str] = mapped_column(String(64))
    region: Mapped[str] = mapped_column(String(8))
    interval: Mapped[str] = mapped_column(String(8))
    bar_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    bar_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    trading_date: Mapped[date] = mapped_column(Date)
    open: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    high: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    low: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    close: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    volume: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    adjustment: Mapped[str] = mapped_column(String(32))
    provider_id: Mapped[str] = mapped_column(String(64))
    provider_record_id: Mapped[str] = mapped_column(String(256))
    ingestion_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_runs.run_id", ondelete="RESTRICT"), nullable=True, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MarketObservationRow(Base):
    __tablename__ = "market_observations"
    __table_args__ = (
        Index("ix_market_observations_metric_available", "metric_code", "available_at"),
    )

    observation_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    region: Mapped[str] = mapped_column(String(8), index=True)
    metric_code: Mapped[str] = mapped_column(String(128), index=True)
    scope_id: Mapped[str] = mapped_column(String(128), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provider_id: Mapped[str] = mapped_column(String(64))
    provider_record_id: Mapped[str] = mapped_column(String(256))
    ingestion_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_runs.run_id", ondelete="RESTRICT"), nullable=True, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MacroSeriesRow(Base):
    __tablename__ = "macro_series"

    series_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    region: Mapped[str] = mapped_column(String(8), index=True)
    ingestion_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_runs.run_id", ondelete="RESTRICT"), nullable=True, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MacroObservationRow(Base):
    __tablename__ = "macro_observations"
    __table_args__ = (
        UniqueConstraint(
            "series_id",
            "period_end",
            "vintage_id",
            "provider_id",
            name="uq_macro_observation_vintage",
        ),
        Index("ix_macro_observations_series_available", "series_id", "available_at"),
    )

    observation_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    series_id: Mapped[str] = mapped_column(
        ForeignKey("macro_series.series_id", ondelete="RESTRICT")
    )
    region: Mapped[str] = mapped_column(String(8))
    period_end: Mapped[date] = mapped_column(Date)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    vintage_id: Mapped[str] = mapped_column(String(128))
    revision_no: Mapped[int] = mapped_column(Integer)
    provider_id: Mapped[str] = mapped_column(String(64))
    ingestion_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_runs.run_id", ondelete="RESTRICT"), nullable=True, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MacroReleaseRow(Base):
    __tablename__ = "macro_releases"
    __table_args__ = (Index("ix_macro_releases_region_scheduled", "region", "scheduled_at"),)

    release_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    series_id: Mapped[str] = mapped_column(
        ForeignKey("macro_series.series_id", ondelete="RESTRICT")
    )
    region: Mapped[str] = mapped_column(String(8))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ingestion_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_runs.run_id", ondelete="RESTRICT"), nullable=True, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MacroReleaseRevisionRow(Base):
    __tablename__ = "macro_release_revisions"
    __table_args__ = (
        UniqueConstraint("release_id", "source_checksum_sha256", name="uq_macro_release_revision"),
        Index("ix_macro_release_revisions_release_available", "release_id", "available_at"),
    )

    revision_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    release_id: Mapped[str] = mapped_column(
        ForeignKey("macro_releases.release_id", ondelete="RESTRICT"), index=True
    )
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_checksum_sha256: Mapped[str] = mapped_column(String(64))
    ingestion_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_runs.run_id", ondelete="RESTRICT"), nullable=True, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class NewsEventRow(Base):
    __tablename__ = "news_events"
    __table_args__ = (
        Index("ix_news_events_published", "published_at", "news_id"),
        Index("ix_news_events_available", "available_at"),
        Index("ix_news_events_cluster", "cluster_id"),
    )

    news_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    cluster_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    provider_id: Mapped[str] = mapped_column(String(64))
    provider_record_id: Mapped[str] = mapped_column(String(256))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24))
    ingestion_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_runs.run_id", ondelete="RESTRICT"), nullable=True, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class NewsEventRegionRow(Base):
    __tablename__ = "news_event_regions"
    news_id: Mapped[str] = mapped_column(
        ForeignKey("news_events.news_id", ondelete="CASCADE"), primary_key=True
    )
    region: Mapped[str] = mapped_column(String(8), primary_key=True)


class NewsEventEntityRow(Base):
    __tablename__ = "news_event_entities"
    news_id: Mapped[str] = mapped_column(
        ForeignKey("news_events.news_id", ondelete="CASCADE"), primary_key=True
    )
    entity_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(32))


class NewsEventTopicRow(Base):
    __tablename__ = "news_event_topics"
    news_id: Mapped[str] = mapped_column(
        ForeignKey("news_events.news_id", ondelete="CASCADE"), primary_key=True
    )
    topic: Mapped[str] = mapped_column(String(128), primary_key=True)


class ProviderRunRow(Base):
    __tablename__ = "provider_runs"
    __table_args__ = (
        Index("ix_provider_runs_role_started", "provider_role", "started_at"),
        Index("uq_provider_runs_idempotency", "idempotency_key", unique=True),
    )

    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_role: Mapped[str] = mapped_column(String(64))
    dataset: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24))
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    records_fetched: Mapped[int] = mapped_column(Integer, default=0)
    records_accepted: Mapped[int] = mapped_column(Integer, default=0)
    records_rejected: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ReportInputSnapshotRow(Base):
    __tablename__ = "report_input_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "report_date",
            "snapshot_version",
            "fingerprint_sha256",
            name="uq_report_snapshot_identity",
        ),
        Index("ix_report_snapshots_report_date", "report_date", "created_at"),
    )

    snapshot_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    snapshot_version: Mapped[str] = mapped_column(String(32))
    report_date: Mapped[date] = mapped_column(Date)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fingerprint_sha256: Mapped[str] = mapped_column(String(64))
    fact_ids: Mapped[list[str]] = mapped_column(JSONB)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReportGenerationAttemptRow(Base):
    __tablename__ = "report_generation_attempts"
    __table_args__ = (
        UniqueConstraint("report_id", "report_version", name="uq_report_generation_report_version"),
        CheckConstraint(
            "lifecycle_status IN ('draft', 'generated', 'failed', 'validated', 'superseded')",
            name="ck_report_generation_lifecycle_status",
        ),
        CheckConstraint("attempt_no >= 1", name="ck_report_generation_attempt_no_positive"),
        Index("ix_report_generation_snapshot", "input_snapshot_id", "created_at"),
        Index("ix_report_generation_status", "lifecycle_status", "updated_at"),
    )

    generation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    report_id: Mapped[str] = mapped_column(String(128))
    report_version: Mapped[str] = mapped_column(String(64))
    input_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("report_input_snapshots.snapshot_id", ondelete="RESTRICT"), index=True
    )
    lifecycle_status: Mapped[str] = mapped_column(String(24))
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    prompt_version: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    model_parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    input_fingerprint_sha256: Mapped[str] = mapped_column(String(64))
    source_ref_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DailyReportRow(Base):
    __tablename__ = "daily_reports"
    __table_args__ = (
        UniqueConstraint("report_date", "report_version", name="uq_daily_report_date_version"),
        CheckConstraint(
            "lifecycle_status IN ('draft', 'generated', 'failed', 'validated', 'superseded')",
            name="ck_daily_reports_lifecycle_status",
        ),
        Index("ix_daily_reports_date_created", "report_date", "created_at"),
    )

    report_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    report_date: Mapped[date] = mapped_column(Date)
    report_version: Mapped[str] = mapped_column(String(64))
    contract_version: Mapped[str] = mapped_column(String(32))
    input_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("report_input_snapshots.snapshot_id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[str] = mapped_column(String(24))
    publication_decision: Mapped[str] = mapped_column(String(24))
    lifecycle_status: Mapped[str] = mapped_column(String(24), default="generated")
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    generation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("report_generation_attempts.generation_id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DailyReportSourceRefRow(Base):
    __tablename__ = "daily_report_source_refs"
    __table_args__ = (
        Index("ix_daily_report_source_refs_provider_record", "provider_id", "provider_record_id"),
        Index("ix_daily_report_source_refs_checksum", "checksum_sha256"),
        Index("ix_daily_report_source_refs_retrieved", "retrieved_at"),
    )

    report_id: Mapped[str] = mapped_column(
        ForeignKey("daily_reports.report_id", ondelete="CASCADE"), primary_key=True
    )
    source_ref_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(64))
    provider_record_id: Mapped[str] = mapped_column(String(256))
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DeliveryAttemptRow(Base):
    __tablename__ = "delivery_attempts"
    __table_args__ = (
        UniqueConstraint(
            "report_id",
            "delivery_target",
            "idempotency_key",
            name="uq_delivery_attempt_idempotency",
        ),
        Index("ix_delivery_attempts_report_status", "report_id", "status"),
    )

    delivery_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    report_id: Mapped[str] = mapped_column(
        ForeignKey("daily_reports.report_id", ondelete="RESTRICT"), index=True
    )
    delivery_target: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24))
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class JobWatermarkRow(Base):
    __tablename__ = "job_watermarks"

    provider_role: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(64), primary_key=True)
    region: Mapped[str] = mapped_column(String(8), primary_key=True)
    watermark: Mapped[str | None] = mapped_column(Text, nullable=True)
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IngestPageCommitRow(Base):
    __tablename__ = "ingest_page_commits"

    provider_role: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(64), primary_key=True)
    region: Mapped[str] = mapped_column(String(8), primary_key=True)
    page_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_watermark: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    accepted_record_ids: Mapped[list[str]] = mapped_column(JSONB)
    ingestion_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_runs.run_id", ondelete="RESTRICT"), nullable=True, index=True
    )
    committed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class IngestAuditRow(Base):
    __tablename__ = "ingest_audits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    provider_id: Mapped[str] = mapped_column(String(64), index=True)
    audit_kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IngestRejectionRow(Base):
    __tablename__ = "ingest_rejections"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    provider_id: Mapped[str] = mapped_column(String(64))
    error_code: Mapped[str] = mapped_column(String(64))
    redacted_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContextBuildRow(Base):
    __tablename__ = "context_builds"

    context_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    data_fingerprint_sha256: Mapped[str] = mapped_column(String(64), index=True)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    coverage_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
