"""Persist Trustpilot identity cache and review observation history.

Revision ID: 20260821_0005
Revises: 20260821_0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260821_0005"
down_revision: str | None = "20260821_0004"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ads", sa.Column("landing_page_url", sa.Text(), nullable=True))
    op.add_column(
        "ads", sa.Column("landing_page_domain", sa.String(500), nullable=True)
    )
    for column in (
        sa.Column("trustpilot_business_unit_id", sa.String(255), nullable=True),
        sa.Column("trustpilot_matched_domain", sa.String(500), nullable=True),
        sa.Column("trustpilot_last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latest_trustpilot_review_count", sa.Integer(), nullable=True),
        sa.Column("latest_trustpilot_trust_score", sa.Numeric(3, 2), nullable=True),
        sa.Column("latest_trustpilot_stars", sa.Numeric(2, 1), nullable=True),
    ):
        op.add_column("advertisers", column)
    for column in (
        sa.Column("review_source", sa.String(50), nullable=True),
        sa.Column("review_count", sa.Integer(), nullable=True),
        sa.Column("review_trust_score", sa.Numeric(3, 2), nullable=True),
        sa.Column("review_stars", sa.Numeric(2, 1), nullable=True),
        sa.Column("review_business_unit_id", sa.String(255), nullable=True),
        sa.Column("review_matched_domain", sa.String(500), nullable=True),
        sa.Column("review_desirable", sa.Boolean(), nullable=True),
        sa.Column("review_status", sa.String(20), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
    ):
        op.add_column("advertiser_observations", column)


def downgrade() -> None:
    for column in (
        "review_reason",
        "review_status",
        "review_desirable",
        "review_matched_domain",
        "review_business_unit_id",
        "review_stars",
        "review_trust_score",
        "review_count",
        "review_source",
    ):
        op.drop_column("advertiser_observations", column)
    for column in (
        "latest_trustpilot_stars",
        "latest_trustpilot_trust_score",
        "latest_trustpilot_review_count",
        "trustpilot_last_refreshed_at",
        "trustpilot_matched_domain",
        "trustpilot_business_unit_id",
    ):
        op.drop_column("advertisers", column)
    op.drop_column("ads", "landing_page_domain")
    op.drop_column("ads", "landing_page_url")
