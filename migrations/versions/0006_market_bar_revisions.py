"""Preserve first-seen market bars and append later source revisions.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_bar_revisions",
        sa.Column("revision_id", sa.String(96), primary_key=True),
        sa.Column(
            "bar_id",
            sa.String(96),
            sa.ForeignKey("market_bars.bar_id", ondelete="RESTRICT"),
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
            "bar_id",
            "available_at",
            "source_checksum_sha256",
            name="uq_market_bar_revision",
        ),
    )
    op.create_index("ix_market_bar_revisions_bar_id", "market_bar_revisions", ["bar_id"])
    op.create_index(
        "ix_market_bar_revisions_bar_available",
        "market_bar_revisions",
        ["bar_id", "available_at"],
    )
    op.create_index(
        "ix_market_bar_revisions_ingestion_run_id",
        "market_bar_revisions",
        ["ingestion_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_market_bar_revisions_ingestion_run_id", table_name="market_bar_revisions")
    op.drop_index("ix_market_bar_revisions_bar_available", table_name="market_bar_revisions")
    op.drop_index("ix_market_bar_revisions_bar_id", table_name="market_bar_revisions")
    op.drop_table("market_bar_revisions")
