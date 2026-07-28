"""Add protected delivery operator action audit.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "delivery_operator_actions",
        sa.Column("action_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_id", sa.String(128), nullable=False),
        sa.Column("delivery_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("confirmed_not_delivered", sa.Boolean(), nullable=False),
        sa.Column("prior_status", sa.String(24), nullable=False),
        sa.Column("prior_attempt_no", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("result_delivery_status", sa.String(24), nullable=True),
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
            "action IN ('retry')",
            name="ck_delivery_operator_actions_action",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'rejected', 'failed')",
            name="ck_delivery_operator_actions_status",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["daily_reports.report_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["delivery_attempts.delivery_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("action_id"),
        sa.UniqueConstraint("request_id", name="uq_delivery_operator_action_request"),
    )
    op.create_index(
        "ix_delivery_operator_actions_report",
        "delivery_operator_actions",
        ["report_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_delivery_operator_actions_report",
        table_name="delivery_operator_actions",
    )
    op.drop_table("delivery_operator_actions")
