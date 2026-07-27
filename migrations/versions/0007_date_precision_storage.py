"""Persist date-only macro releases and news events without inventing timestamps.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("macro_releases", "scheduled_at", existing_type=sa.DateTime(timezone=True), nullable=True)
    op.add_column("macro_releases", sa.Column("scheduled_date", sa.Date(), nullable=True))
    op.create_index(
        "ix_macro_releases_region_scheduled_date",
        "macro_releases",
        ["region", "scheduled_date"],
    )

    op.alter_column("news_events", "published_at", existing_type=sa.DateTime(timezone=True), nullable=True)
    op.add_column("news_events", sa.Column("published_date", sa.Date(), nullable=True))
    op.create_index("ix_news_events_published_date", "news_events", ["published_date", "news_id"])


def downgrade() -> None:
    # A downgrade cannot keep the date-precision fields. Preserve their date
    # deterministically in the legacy timestamp columns before restoring the
    # old non-null schema.
    op.execute(
        "UPDATE macro_releases SET scheduled_at = scheduled_date::timestamp AT TIME ZONE 'UTC' "
        "WHERE scheduled_at IS NULL AND scheduled_date IS NOT NULL"
    )
    op.execute(
        "UPDATE news_events SET published_at = published_date::timestamp AT TIME ZONE 'UTC' "
        "WHERE published_at IS NULL AND published_date IS NOT NULL"
    )
    op.drop_index("ix_news_events_published_date", table_name="news_events")
    op.drop_column("news_events", "published_date")
    op.alter_column("news_events", "published_at", existing_type=sa.DateTime(timezone=True), nullable=False)
    op.drop_index("ix_macro_releases_region_scheduled_date", table_name="macro_releases")
    op.drop_column("macro_releases", "scheduled_date")
    op.alter_column(
        "macro_releases", "scheduled_at", existing_type=sa.DateTime(timezone=True), nullable=False
    )
