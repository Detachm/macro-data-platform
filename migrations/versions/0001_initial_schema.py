"""Create the canonical data foundation tables.

Revision ID: 0001
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("instrument_id", sa.String(64), primary_key=True),
        sa.Column("canonical_symbol", sa.String(64), nullable=False, unique=True),
        sa.Column("region", sa.String(8), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_instruments_canonical_symbol", "instruments", ["canonical_symbol"])
    op.create_index("ix_instruments_region", "instruments", ["region"])
    op.create_index("ix_instruments_status", "instruments", ["status"])

    op.create_table(
        "instrument_aliases",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "instrument_id",
            sa.String(64),
            sa.ForeignKey("instruments.instrument_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("source_symbol", sa.String(128), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.UniqueConstraint(
            "provider_id", "source_symbol", "valid_from", name="uq_instrument_alias_effective"
        ),
    )
    op.create_index("ix_instrument_aliases_instrument_id", "instrument_aliases", ["instrument_id"])
    op.create_index("ix_instrument_aliases_provider_id", "instrument_aliases", ["provider_id"])

    op.create_table(
        "market_bars",
        sa.Column("bar_id", sa.String(96), primary_key=True),
        sa.Column(
            "instrument_id",
            sa.String(64),
            sa.ForeignKey("instruments.instrument_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("canonical_symbol", sa.String(64), nullable=False),
        sa.Column("region", sa.String(8), nullable=False),
        sa.Column("interval", sa.String(8), nullable=False),
        sa.Column("bar_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bar_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(38, 18), nullable=False),
        sa.Column("high", sa.Numeric(38, 18), nullable=False),
        sa.Column("low", sa.Numeric(38, 18), nullable=False),
        sa.Column("close", sa.Numeric(38, 18), nullable=False),
        sa.Column("volume", sa.Numeric(38, 18), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("adjustment", sa.String(32), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("provider_record_id", sa.String(256), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "instrument_id",
            "interval",
            "bar_start",
            "adjustment",
            "provider_id",
            name="uq_market_bar_source",
        ),
    )
    op.create_index("ix_market_bars_instrument_end", "market_bars", ["instrument_id", "bar_end"])
    op.create_index("ix_market_bars_available", "market_bars", ["available_at"])

    op.create_table(
        "market_observations",
        sa.Column("observation_id", sa.String(96), primary_key=True),
        sa.Column("region", sa.String(8), nullable=False),
        sa.Column("metric_code", sa.String(128), nullable=False),
        sa.Column("scope_id", sa.String(128), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("provider_record_id", sa.String(256), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_market_observations_metric_available",
        "market_observations",
        ["metric_code", "available_at"],
    )
    op.create_index("ix_market_observations_region", "market_observations", ["region"])
    op.create_index("ix_market_observations_scope_id", "market_observations", ["scope_id"])

    op.create_table(
        "macro_series",
        sa.Column("series_id", sa.String(160), primary_key=True),
        sa.Column("region", sa.String(8), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_macro_series_region", "macro_series", ["region"])

    op.create_table(
        "macro_observations",
        sa.Column("observation_id", sa.String(96), primary_key=True),
        sa.Column(
            "series_id",
            sa.String(160),
            sa.ForeignKey("macro_series.series_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("region", sa.String(8), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("vintage_id", sa.String(128), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "series_id",
            "period_end",
            "vintage_id",
            "provider_id",
            name="uq_macro_observation_vintage",
        ),
    )
    op.create_index(
        "ix_macro_observations_series_available",
        "macro_observations",
        ["series_id", "available_at"],
    )

    op.create_table(
        "macro_releases",
        sa.Column("release_id", sa.String(128), primary_key=True),
        sa.Column(
            "series_id",
            sa.String(160),
            sa.ForeignKey("macro_series.series_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("region", sa.String(8), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_macro_releases_region_scheduled", "macro_releases", ["region", "scheduled_at"]
    )

    op.create_table(
        "news_events",
        sa.Column("news_id", sa.String(96), primary_key=True),
        sa.Column("cluster_id", sa.String(96), nullable=True),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("provider_record_id", sa.String(256), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_news_events_published", "news_events", ["published_at", "news_id"])
    op.create_index("ix_news_events_available", "news_events", ["available_at"])
    op.create_index("ix_news_events_cluster", "news_events", ["cluster_id"])

    op.create_table(
        "news_event_regions",
        sa.Column(
            "news_id",
            sa.String(96),
            sa.ForeignKey("news_events.news_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("region", sa.String(8), primary_key=True),
    )
    op.create_table(
        "news_event_entities",
        sa.Column(
            "news_id",
            sa.String(96),
            sa.ForeignKey("news_events.news_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("entity_id", sa.String(128), primary_key=True),
        sa.Column("entity_type", sa.String(32), nullable=False),
    )
    op.create_table(
        "news_event_topics",
        sa.Column(
            "news_id",
            sa.String(96),
            sa.ForeignKey("news_events.news_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("topic", sa.String(128), primary_key=True),
    )

    op.create_table(
        "provider_runs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider_role", sa.String(64), nullable=False),
        sa.Column("dataset", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("records_fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_accepted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.create_index(
        "ix_provider_runs_role_started", "provider_runs", ["provider_role", "started_at"]
    )

    op.create_table(
        "job_watermarks",
        sa.Column("provider_role", sa.String(64), primary_key=True),
        sa.Column("dataset", sa.String(64), primary_key=True),
        sa.Column("region", sa.String(8), primary_key=True),
        sa.Column("watermark", sa.Text(), nullable=True),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "ingest_rejections",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=False),
        sa.Column("redacted_payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_ingest_rejections_run_id", "ingest_rejections", ["run_id"])

    op.create_table(
        "context_builds",
        sa.Column("context_id", sa.String(96), primary_key=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(), nullable=False),
        sa.Column("coverage_payload", postgresql.JSONB(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_context_builds_as_of", "context_builds", ["as_of"])
    op.create_index("ix_context_builds_fingerprint", "context_builds", ["data_fingerprint_sha256"])


def downgrade() -> None:
    op.drop_table("context_builds")
    op.drop_table("ingest_rejections")
    op.drop_table("job_watermarks")
    op.drop_table("provider_runs")
    op.drop_table("news_event_topics")
    op.drop_table("news_event_entities")
    op.drop_table("news_event_regions")
    op.drop_table("news_events")
    op.drop_table("macro_releases")
    op.drop_table("macro_observations")
    op.drop_table("macro_series")
    op.drop_table("market_observations")
    op.drop_table("market_bars")
    op.drop_table("instrument_aliases")
    op.drop_table("instruments")
