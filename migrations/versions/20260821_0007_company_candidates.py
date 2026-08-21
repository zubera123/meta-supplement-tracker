"""Add canonical companies, candidate lifecycle, and stable Sheet metadata.

Revision ID: 20260821_0007
Revises: 20260821_0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260821_0007"
down_revision: str | None = "20260821_0006"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("scan_runs", sa.Column("coverage_complete", sa.Boolean(), server_default=sa.true(), nullable=False))
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("canonical_domain", sa.String(500), unique=True),
        sa.Column("display_name", sa.String(500), nullable=False),
        sa.Column("regions", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consecutive_disqualifications", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consecutive_absent_successful_scans", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sheet_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("merged_into_company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="SET NULL")),
    )
    op.execute(
        "INSERT INTO companies (id, display_name, first_seen_at, last_seen_at, sheet_eligible) "
        "SELECT id, page_name, first_seen_at, last_seen_at, EXISTS "
        "(SELECT 1 FROM google_sheet_rows g WHERE g.advertiser_id=advertisers.id) FROM advertisers"
    )
    op.execute(
        "SELECT setval(pg_get_serial_sequence('companies','id'), "
        "COALESCE((SELECT MAX(id) FROM companies), 1), EXISTS (SELECT 1 FROM companies))"
    )
    op.add_column("advertisers", sa.Column("company_id", sa.Integer(), nullable=True))
    op.add_column("advertisers", sa.Column("verified_landing_domain", sa.String(500)))
    op.add_column("advertisers", sa.Column("company_mapping_reason", sa.String(500), server_default="legacy advertiser identity", nullable=False))
    op.execute("UPDATE advertisers SET company_id=id")
    op.alter_column("advertisers", "company_id", nullable=False)
    op.create_foreign_key("fk_advertisers_company_id_companies", "advertisers", "companies", ["company_id"], ["id"], ondelete="RESTRICT")
    op.create_table(
        "advertiser_company_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("advertiser_id", sa.Integer(), sa.ForeignKey("advertisers.id"), nullable=False),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("scan_run_id", sa.Integer(), sa.ForeignKey("scan_runs.id")),
        sa.Column("verified_domain", sa.String(500)),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        "INSERT INTO advertiser_company_mappings "
        "(advertiser_id, company_id, verified_domain, reason, started_at) "
        "SELECT id, company_id, verified_landing_domain, company_mapping_reason, first_seen_at FROM advertisers"
    )

    op.add_column("google_sheet_rows", sa.Column("company_id", sa.Integer(), nullable=True))
    op.add_column("google_sheet_rows", sa.Column("developer_metadata_id", sa.Integer()))
    op.execute("UPDATE google_sheet_rows SET company_id=advertiser_id")
    op.alter_column("google_sheet_rows", "company_id", nullable=False)
    op.create_foreign_key("fk_google_sheet_rows_company_id_companies", "google_sheet_rows", "companies", ["company_id"], ["id"], ondelete="CASCADE")
    op.create_unique_constraint("uq_google_sheet_rows_company_id", "google_sheet_rows", ["company_id"])
    op.create_unique_constraint("uq_google_sheet_rows_developer_metadata_id", "google_sheet_rows", ["developer_metadata_id"])
    op.drop_constraint("uq_google_sheet_rows_advertiser_id", "google_sheet_rows", type_="unique")
    op.drop_constraint("fk_google_sheet_rows_advertiser_id_advertisers", "google_sheet_rows", type_="foreignkey")
    op.drop_column("google_sheet_rows", "advertiser_id")

    op.create_table(
        "company_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("scan_run_id", sa.Integer(), sa.ForeignKey("scan_runs.id"), nullable=False),
        sa.Column("explicitly_disqualified", sa.Boolean()),
        sa.Column("disqualification_reasons", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_id", "scan_run_id"),
    )
    op.create_table(
        "company_candidate_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("scan_run_id", sa.Integer(), sa.ForeignKey("scan_runs.id")),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("company_candidate_events")
    op.drop_table("company_observations")
    op.add_column("google_sheet_rows", sa.Column("advertiser_id", sa.Integer(), nullable=True))
    op.execute("UPDATE google_sheet_rows SET advertiser_id=company_id")
    op.alter_column("google_sheet_rows", "advertiser_id", nullable=False)
    op.create_foreign_key("fk_google_sheet_rows_advertiser_id_advertisers", "google_sheet_rows", "advertisers", ["advertiser_id"], ["id"], ondelete="CASCADE")
    op.create_unique_constraint("uq_google_sheet_rows_advertiser_id", "google_sheet_rows", ["advertiser_id"])
    op.drop_constraint("uq_google_sheet_rows_developer_metadata_id", "google_sheet_rows", type_="unique")
    op.drop_constraint("uq_google_sheet_rows_company_id", "google_sheet_rows", type_="unique")
    op.drop_constraint("fk_google_sheet_rows_company_id_companies", "google_sheet_rows", type_="foreignkey")
    op.drop_column("google_sheet_rows", "developer_metadata_id")
    op.drop_column("google_sheet_rows", "company_id")
    op.drop_constraint("fk_advertisers_company_id_companies", "advertisers", type_="foreignkey")
    op.drop_table("advertiser_company_mappings")
    op.drop_column("advertisers", "company_mapping_reason")
    op.drop_column("advertisers", "verified_landing_domain")
    op.drop_column("advertisers", "company_id")
    op.drop_table("companies")
    op.drop_column("scan_runs", "coverage_complete")
