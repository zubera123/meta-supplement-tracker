"""Preserve the review provider source in the advertiser cache.

Revision ID: 20260821_0006
Revises: 20260821_0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260821_0006"
down_revision: str | None = "20260821_0005"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "advertisers",
        sa.Column("latest_trustpilot_review_source", sa.String(50), nullable=True),
    )
    op.execute(
        "UPDATE advertisers SET latest_trustpilot_review_source = 'Trustpilot' "
        "WHERE latest_trustpilot_review_count IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("advertisers", "latest_trustpilot_review_source")
