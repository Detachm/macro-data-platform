"""Fence reclaimed scheduled task checkpoints.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scheduled_task_checkpoints",
        sa.Column("lease_epoch", sa.BigInteger(), nullable=False, server_default="1"),
    )
    op.add_column(
        "scheduled_task_checkpoints",
        sa.Column("lease_owner_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_scheduled_task_checkpoint_lease_epoch",
        "scheduled_task_checkpoints",
        "lease_epoch >= 1",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_scheduled_task_checkpoint_lease_epoch",
        "scheduled_task_checkpoints",
        type_="check",
    )
    op.drop_column("scheduled_task_checkpoints", "lease_owner_id")
    op.drop_column("scheduled_task_checkpoints", "lease_epoch")
