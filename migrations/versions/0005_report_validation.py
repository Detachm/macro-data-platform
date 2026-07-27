"""Persist report validation errors for operator replay and diagnosis.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "daily_reports",
        sa.Column(
            "validation_errors",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.alter_column("daily_reports", "validation_errors", server_default=None)


def downgrade() -> None:
    op.drop_column("daily_reports", "validation_errors")
