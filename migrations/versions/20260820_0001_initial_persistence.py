"""Create scan persistence tables.

Revision ID: 20260820_0001
Revises:
Create Date: 2026-08-20 00:00:00+00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260820_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scan_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("regions", sa.JSON(), nullable=False),
        sa.Column("ads_found", sa.Integer(), nullable=False),
        sa.Column("advertisers_found", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name=op.f("ck_scan_runs_valid_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scan_runs")),
    )
    op.create_table(
        "advertisers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("meta_page_id", sa.String(length=255), nullable=True),
        sa.Column("page_name", sa.String(length=500), nullable=False),
        sa.Column("instagram_username", sa.String(length=255), nullable=True),
        sa.Column("latest_instagram_followers", sa.Integer(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_advertisers")),
        sa.UniqueConstraint(
            "meta_page_id", name=op.f("uq_advertisers_meta_page_id")
        ),
    )
    op.create_index(
        "ix_advertisers_instagram_username",
        "advertisers",
        ["instagram_username"],
        unique=False,
    )
    op.create_table(
        "ads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("meta_ad_id", sa.String(length=255), nullable=False),
        sa.Column("advertiser_id", sa.Integer(), nullable=False),
        sa.Column("ad_start_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ad_text", sa.Text(), nullable=True),
        sa.Column("snapshot_url", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["advertiser_id"],
            ["advertisers.id"],
            name=op.f("fk_ads_advertiser_id_advertisers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ads")),
        sa.UniqueConstraint("meta_ad_id", name=op.f("uq_ads_meta_ad_id")),
    )
    op.create_table(
        "advertiser_observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("advertiser_id", sa.Integer(), nullable=False),
        sa.Column("scan_run_id", sa.Integer(), nullable=False),
        sa.Column("instagram_followers", sa.Integer(), nullable=True),
        sa.Column("active_ad_count", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["advertiser_id"],
            ["advertisers.id"],
            name=op.f(
                "fk_advertiser_observations_advertiser_id_advertisers"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["scan_run_id"],
            ["scan_runs.id"],
            name=op.f("fk_advertiser_observations_scan_run_id_scan_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_advertiser_observations")
        ),
        sa.UniqueConstraint(
            "advertiser_id",
            "scan_run_id",
            name=op.f(
                "uq_advertiser_observations_advertiser_id"
            ),
        ),
    )
    op.create_index(
        "ix_advertiser_observations_observed_at",
        "advertiser_observations",
        ["observed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_advertiser_observations_observed_at",
        table_name="advertiser_observations",
    )
    op.drop_table("advertiser_observations")
    op.drop_table("ads")
    op.drop_index("ix_advertisers_instagram_username", table_name="advertisers")
    op.drop_table("advertisers")
    op.drop_table("scan_runs")
