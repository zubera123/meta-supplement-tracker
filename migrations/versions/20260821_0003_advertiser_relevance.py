"""Record advertiser relevance decisions on observations.

Revision ID: 20260821_0003
Revises: 20260821_0002
Create Date: 2026-08-21 00:00:00+00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260821_0003"
down_revision: str | Sequence[str] | None = "20260821_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "advertiser_observations",
        sa.Column("supplement_relevant", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "advertiser_observations",
        sa.Column("relevance_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("advertiser_observations", "relevance_reason")
    op.drop_column("advertiser_observations", "supplement_relevant")
