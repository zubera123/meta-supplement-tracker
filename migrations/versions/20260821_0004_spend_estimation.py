"""Store spend-estimation history on advertiser observations.

Revision ID: 20260821_0004
Revises: 20260821_0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260821_0004"
down_revision: str | None = "20260821_0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("advertiser_observations", sa.Column("spend_estimate_low_usd", sa.Numeric(12, 2), nullable=True))
    op.add_column("advertiser_observations", sa.Column("spend_estimate_high_usd", sa.Numeric(12, 2), nullable=True))
    op.add_column("advertiser_observations", sa.Column("spend_estimation_method", sa.String(50), nullable=True))
    op.add_column("advertiser_observations", sa.Column("spend_estimation_source", sa.String(50), nullable=True))
    op.add_column("advertiser_observations", sa.Column("spend_estimation_confidence", sa.String(10), nullable=True))
    op.add_column("advertiser_observations", sa.Column("spend_estimation_inputs", sa.JSON(), nullable=True))
    op.add_column("advertiser_observations", sa.Column("spend_estimation_assumptions", sa.JSON(), nullable=True))
    op.add_column("advertiser_observations", sa.Column("spend_target_match", sa.Boolean(), nullable=True))


def downgrade() -> None:
    for column in (
        "spend_target_match",
        "spend_estimation_assumptions",
        "spend_estimation_inputs",
        "spend_estimation_confidence",
        "spend_estimation_source",
        "spend_estimation_method",
        "spend_estimate_high_usd",
        "spend_estimate_low_usd",
    ):
        op.drop_column("advertiser_observations", column)
