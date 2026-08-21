"""Add the durable UTC-day Trustpilot paid-lookup ledger.

Revision ID: 20260821_0008
Revises: 20260821_0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260821_0008"
down_revision: str | None = "20260821_0007"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "advertisers",
        sa.Column("latest_trustpilot_profile_url", sa.Text(), nullable=True),
    )
    op.add_column(
        "advertiser_observations",
        sa.Column("review_profile_url", sa.Text(), nullable=True),
    )
    op.create_table(
        "trustpilot_paid_lookups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lookup_date", sa.Date(), nullable=False),
        sa.Column("domain", sa.String(500), nullable=False),
        sa.Column(
            "reserved_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.UniqueConstraint("lookup_date", "domain"),
    )
    op.create_index(
        "ix_trustpilot_paid_lookups_lookup_date",
        "trustpilot_paid_lookups",
        ["lookup_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trustpilot_paid_lookups_lookup_date",
        table_name="trustpilot_paid_lookups",
    )
    op.drop_table("trustpilot_paid_lookups")
    op.drop_column("advertiser_observations", "review_profile_url")
    op.drop_column("advertisers", "latest_trustpilot_profile_url")
