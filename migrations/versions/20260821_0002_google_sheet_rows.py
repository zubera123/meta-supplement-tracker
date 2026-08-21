"""Add PostgreSQL-only Google Sheet row mappings.

Revision ID: 20260821_0002
Revises: 20260820_0001
Create Date: 2026-08-21 00:00:00+00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260821_0002"
down_revision: str | Sequence[str] | None = "20260820_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "google_sheet_rows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("advertiser_id", sa.Integer(), nullable=False),
        sa.Column("spreadsheet_id", sa.String(length=255), nullable=False),
        sa.Column("sheet_tab", sa.String(length=100), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("last_exported_first_seen", sa.Date(), nullable=False),
        sa.Column("last_exported_brand", sa.String(length=500), nullable=False),
        sa.Column("last_exported_region", sa.String(length=500), nullable=True),
        sa.Column("last_exported_instagram", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["advertiser_id"],
            ["advertisers.id"],
            name=op.f("fk_google_sheet_rows_advertiser_id_advertisers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_google_sheet_rows")),
        sa.UniqueConstraint(
            "advertiser_id", name=op.f("uq_google_sheet_rows_advertiser_id")
        ),
    )


def downgrade() -> None:
    op.drop_table("google_sheet_rows")
