"""Add report generation attempts and model audit trace.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "report_generation_attempts",
        sa.Column("generation_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("report_id", sa.String(128), nullable=False),
        sa.Column("report_version", sa.String(64), nullable=False),
        sa.Column(
            "input_snapshot_id",
            sa.String(128),
            sa.ForeignKey("report_input_snapshots.snapshot_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("lifecycle_status", sa.String(24), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column(
            "model_parameters",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("input_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column(
            "source_ref_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("response_payload", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "report_id", "report_version", name="uq_report_generation_report_version"
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('draft', 'generated', 'failed', 'validated', 'superseded')",
            name="ck_report_generation_lifecycle_status",
        ),
        sa.CheckConstraint("attempt_no >= 1", name="ck_report_generation_attempt_no_positive"),
    )
    op.create_index(
        "ix_report_generation_snapshot",
        "report_generation_attempts",
        ["input_snapshot_id", "created_at"],
    )
    op.create_index(
        "ix_report_generation_status",
        "report_generation_attempts",
        ["lifecycle_status", "updated_at"],
    )
    op.add_column(
        "daily_reports",
        sa.Column("generation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "daily_reports",
        sa.Column(
            "lifecycle_status",
            sa.String(24),
            nullable=False,
            server_default="generated",
        ),
    )
    op.create_check_constraint(
        "ck_daily_reports_lifecycle_status",
        "daily_reports",
        "lifecycle_status IN ('draft', 'generated', 'failed', 'validated', 'superseded')",
    )
    op.alter_column("daily_reports", "lifecycle_status", server_default=None)
    op.create_foreign_key(
        "fk_daily_reports_generation",
        "daily_reports",
        "report_generation_attempts",
        ["generation_id"],
        ["generation_id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_daily_reports_generation_id", "daily_reports", ["generation_id"])


def downgrade() -> None:
    op.drop_constraint("ck_daily_reports_lifecycle_status", "daily_reports", type_="check")
    op.drop_column("daily_reports", "lifecycle_status")
    op.drop_index("ix_daily_reports_generation_id", table_name="daily_reports")
    op.drop_constraint("fk_daily_reports_generation", "daily_reports", type_="foreignkey")
    op.drop_column("daily_reports", "generation_id")
    op.drop_index("ix_report_generation_status", table_name="report_generation_attempts")
    op.drop_index("ix_report_generation_snapshot", table_name="report_generation_attempts")
    op.drop_table("report_generation_attempts")
