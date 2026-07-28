"""Add durable report-date task checkpoints.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduled_task_checkpoints",
        sa.Column("report_date", sa.Date(), primary_key=True),
        sa.Column("task_id", sa.String(128), primary_key=True),
        sa.Column("provider_role", sa.String(64), nullable=False),
        sa.Column("dataset", sa.String(64), nullable=False),
        sa.Column("region", sa.String(8), nullable=False),
        sa.Column("request_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("next_cursor", sa.Text(), nullable=True),
        sa.Column("source_watermark", sa.Text(), nullable=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_runs.run_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("records_accepted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('active', 'completed')", name="ck_scheduled_task_checkpoint_status"
        ),
    )
    op.create_index(
        "ix_scheduled_task_checkpoints_status",
        "scheduled_task_checkpoints",
        ["report_date", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_task_checkpoints_status", table_name="scheduled_task_checkpoints")
    op.drop_table("scheduled_task_checkpoints")
