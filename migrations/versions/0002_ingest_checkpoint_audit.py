"""Add durable ingest checkpoint and audit records.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingest_page_commits",
        sa.Column("provider_role", sa.String(64), primary_key=True),
        sa.Column("dataset", sa.String(64), primary_key=True),
        sa.Column("region", sa.String(8), primary_key=True),
        sa.Column("page_fingerprint", sa.String(64), primary_key=True),
        sa.Column("source_watermark", sa.Text(), nullable=True),
        sa.Column("next_cursor", sa.Text(), nullable=True),
        sa.Column("accepted_record_ids", postgresql.JSONB(), nullable=False),
        sa.Column(
            "committed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "ingest_audits",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("audit_kind", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_ingest_audits_run_id", "ingest_audits", ["run_id"])
    op.create_index("ix_ingest_audits_provider_id", "ingest_audits", ["provider_id"])


def downgrade() -> None:
    op.drop_table("ingest_audits")
    op.drop_table("ingest_page_commits")
