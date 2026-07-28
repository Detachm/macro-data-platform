"""Add Feishu delivery audit fields to durable report delivery attempts.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "delivery_attempts",
        sa.Column("report_version", sa.String(64), nullable=True),
    )
    op.execute(
        "UPDATE delivery_attempts AS delivery "
        "SET report_version = report.report_version "
        "FROM daily_reports AS report "
        "WHERE delivery.report_id = report.report_id"
    )
    op.alter_column("delivery_attempts", "report_version", nullable=False)
    op.add_column("delivery_attempts", sa.Column("message_id", sa.String(128), nullable=True))
    op.add_column("delivery_attempts", sa.Column("error_code", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("delivery_attempts", "error_code")
    op.drop_column("delivery_attempts", "message_id")
    op.drop_column("delivery_attempts", "report_version")
