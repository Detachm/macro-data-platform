"""Add durable idempotent workflow alert attempts.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_alert_attempts",
        sa.Column("alert_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workflow_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("delivery_target", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column(
            "request_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "response_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("message_id", sa.String(128), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed', 'retry_wait', 'uncertain')",
            name="ck_workflow_alert_attempts_status",
        ),
        sa.PrimaryKeyConstraint("alert_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_workflow_alert_idempotency"),
    )
    op.create_index(
        "ix_workflow_alerts_report_status",
        "workflow_alert_attempts",
        ["report_date", "status"],
    )
    op.create_index(
        "ix_workflow_alerts_run",
        "workflow_alert_attempts",
        ["workflow_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_alerts_run", table_name="workflow_alert_attempts")
    op.drop_index("ix_workflow_alerts_report_status", table_name="workflow_alert_attempts")
    op.drop_table("workflow_alert_attempts")
