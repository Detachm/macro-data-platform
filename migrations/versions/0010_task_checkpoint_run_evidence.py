"""Persist all page-run identities that support a scheduled task result.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scheduled_task_checkpoints",
        sa.Column(
            "run_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.execute(
        """
        UPDATE scheduled_task_checkpoints
        SET run_ids = CASE
            WHEN run_id IS NULL THEN '[]'::jsonb
            ELSE jsonb_build_array(run_id::text)
        END
        """
    )


def downgrade() -> None:
    op.drop_column("scheduled_task_checkpoints", "run_ids")
