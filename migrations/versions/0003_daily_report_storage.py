"""Add durable report, delivery, and ingestion-run idempotency storage.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("provider_runs", sa.Column("idempotency_key", sa.String(128), nullable=True))
    op.add_column(
        "provider_runs",
        sa.Column(
            "request_payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "provider_runs",
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "provider_runs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "uq_provider_runs_idempotency", "provider_runs", ["idempotency_key"], unique=True
    )

    # Existing normalized-fact tables retain full SourceRef as JSONB. These
    # expression indexes make the required raw audit keys queryable without
    # changing public fact contracts or dropping historical payloads.
    for table_name in (
        "instruments",
        "market_bars",
        "market_observations",
        "macro_series",
        "macro_observations",
        "macro_releases",
        "news_events",
    ):
        op.create_index(
            f"ix_{table_name}_source_provider",
            table_name,
            [sa.text("(payload #>> '{source,provider_id}')")],
        )
        op.create_index(
            f"ix_{table_name}_source_record",
            table_name,
            [sa.text("(payload #>> '{source,provider_record_id}')")],
        )
        op.create_index(
            f"ix_{table_name}_source_checksum",
            table_name,
            [sa.text("(payload #>> '{source,checksum_sha256}')")],
        )
        op.create_index(
            f"ix_{table_name}_source_retrieved_at",
            table_name,
            [sa.text("(payload #>> '{source,retrieved_at}')")],
        )

    for table_name in (
        "instruments",
        "market_bars",
        "market_observations",
        "macro_series",
        "macro_observations",
        "macro_releases",
        "news_events",
        "ingest_page_commits",
    ):
        op.add_column(table_name, sa.Column("ingestion_run_id", postgresql.UUID(as_uuid=True)))
        op.create_foreign_key(
            f"fk_{table_name}_ingestion_run",
            table_name,
            "provider_runs",
            ["ingestion_run_id"],
            ["run_id"],
            ondelete="RESTRICT",
        )
        op.create_index(f"ix_{table_name}_ingestion_run_id", table_name, ["ingestion_run_id"])

    # Historical facts predate durable provider-run links. Keep the original
    # source payload intact and leave their ingestion_run_id NULL rather than
    # inventing a run association. New writes always set this field.
    op.add_column(
        "macro_releases", sa.Column("source_checksum_sha256", sa.String(64), nullable=True)
    )
    op.execute(
        "UPDATE macro_releases "
        "SET source_checksum_sha256 = payload #>> '{source,checksum_sha256}' "
        "WHERE source_checksum_sha256 IS NULL"
    )

    op.create_table(
        "macro_release_revisions",
        sa.Column("revision_id", sa.String(96), primary_key=True),
        sa.Column(
            "release_id",
            sa.String(128),
            sa.ForeignKey("macro_releases.release_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_checksum_sha256", sa.String(64), nullable=False),
        sa.Column(
            "ingestion_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_runs.run_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint(
            "release_id", "source_checksum_sha256", name="uq_macro_release_revision"
        ),
    )
    op.create_index(
        "ix_macro_release_revisions_release_available",
        "macro_release_revisions",
        ["release_id", "available_at"],
    )
    op.create_index(
        "ix_macro_release_revisions_ingestion_run_id",
        "macro_release_revisions",
        ["ingestion_run_id"],
    )

    op.create_table(
        "report_input_snapshots",
        sa.Column("snapshot_id", sa.String(128), primary_key=True),
        sa.Column("snapshot_version", sa.String(32), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column("fact_ids", postgresql.JSONB(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "report_date",
            "snapshot_version",
            "fingerprint_sha256",
            name="uq_report_snapshot_identity",
        ),
    )
    op.create_index(
        "ix_report_snapshots_report_date", "report_input_snapshots", ["report_date", "created_at"]
    )

    op.create_table(
        "daily_reports",
        sa.Column("report_id", sa.String(128), primary_key=True),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("report_version", sa.String(64), nullable=False),
        sa.Column("contract_version", sa.String(32), nullable=False),
        sa.Column(
            "input_snapshot_id",
            sa.String(128),
            sa.ForeignKey("report_input_snapshots.snapshot_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("publication_decision", sa.String(24), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("report_date", "report_version", name="uq_daily_report_date_version"),
    )
    op.create_index("ix_daily_reports_input_snapshot_id", "daily_reports", ["input_snapshot_id"])
    op.create_index("ix_daily_reports_date_created", "daily_reports", ["report_date", "created_at"])

    op.create_table(
        "daily_report_source_refs",
        sa.Column(
            "report_id",
            sa.String(128),
            sa.ForeignKey("daily_reports.report_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("source_ref_id", sa.String(128), primary_key=True),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("provider_record_id", sa.String(256), nullable=False),
        sa.Column("checksum_sha256", sa.String(64), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_daily_report_source_refs_provider_record",
        "daily_report_source_refs",
        ["provider_id", "provider_record_id"],
    )
    op.create_index(
        "ix_daily_report_source_refs_checksum", "daily_report_source_refs", ["checksum_sha256"]
    )
    op.create_index(
        "ix_daily_report_source_refs_retrieved", "daily_report_source_refs", ["retrieved_at"]
    )

    op.create_table(
        "delivery_attempts",
        sa.Column("delivery_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "report_id",
            sa.String(128),
            sa.ForeignKey("daily_reports.report_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("delivery_target", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column(
            "request_payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("response_payload", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "report_id",
            "delivery_target",
            "idempotency_key",
            name="uq_delivery_attempt_idempotency",
        ),
    )
    op.create_index("ix_delivery_attempts_report_id", "delivery_attempts", ["report_id"])
    op.create_index(
        "ix_delivery_attempts_report_status", "delivery_attempts", ["report_id", "status"]
    )


def downgrade() -> None:
    op.drop_table("delivery_attempts")
    op.drop_table("daily_report_source_refs")
    op.drop_table("daily_reports")
    op.drop_table("report_input_snapshots")
    op.drop_table("macro_release_revisions")
    op.drop_column("macro_releases", "source_checksum_sha256")
    for table_name in (
        "ingest_page_commits",
        "news_events",
        "macro_releases",
        "macro_observations",
        "macro_series",
        "market_observations",
        "market_bars",
        "instruments",
    ):
        op.drop_constraint(f"fk_{table_name}_ingestion_run", table_name, type_="foreignkey")
        op.drop_index(f"ix_{table_name}_ingestion_run_id", table_name=table_name)
        op.drop_column(table_name, "ingestion_run_id")
    for table_name in (
        "news_events",
        "macro_releases",
        "macro_observations",
        "macro_series",
        "market_observations",
        "market_bars",
        "instruments",
    ):
        op.drop_index(f"ix_{table_name}_source_retrieved_at", table_name=table_name)
        op.drop_index(f"ix_{table_name}_source_checksum", table_name=table_name)
        op.drop_index(f"ix_{table_name}_source_record", table_name=table_name)
        op.drop_index(f"ix_{table_name}_source_provider", table_name=table_name)
    op.drop_index("uq_provider_runs_idempotency", table_name="provider_runs")
    op.drop_column("provider_runs", "lease_expires_at")
    op.drop_column("provider_runs", "attempt_no")
    op.drop_column("provider_runs", "request_payload")
    op.drop_column("provider_runs", "idempotency_key")
