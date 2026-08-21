"""SQLAlchemy models for durable scan history."""

from datetime import UTC, date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utc_now() -> datetime:
    """Return an aware UTC timestamp for Python-side defaults."""

    return datetime.now(UTC)


class ScanRun(Base):
    __tablename__ = "scan_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="valid_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    regions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    ads_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    advertisers_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    coverage_complete: Mapped[bool] = mapped_column(default=True, nullable=False)

    observations: Mapped[list["AdvertiserObservation"]] = relationship(
        back_populates="scan_run", cascade="all, delete-orphan"
    )


class Company(Base):
    """Canonical output identity; original Meta advertisers remain separate."""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_domain: Mapped[str | None] = mapped_column(String(500), unique=True)
    display_name: Mapped[str] = mapped_column(String(500), nullable=False)
    regions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consecutive_disqualifications: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    consecutive_absent_successful_scans: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sheet_eligible: Mapped[bool] = mapped_column(default=False, nullable=False)
    merged_into_company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL")
    )

    advertisers: Mapped[list["Advertiser"]] = relationship(back_populates="company")
    sheet_row: Mapped["GoogleSheetRow | None"] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )


class Advertiser(Base):
    __tablename__ = "advertisers"
    __table_args__ = (
        UniqueConstraint("meta_page_id"),
        Index("ix_advertisers_instagram_username", "instagram_username"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    verified_landing_domain: Mapped[str | None] = mapped_column(String(500))
    company_mapping_reason: Mapped[str] = mapped_column(String(500), nullable=False)
    meta_page_id: Mapped[str | None] = mapped_column(String(255))
    page_name: Mapped[str] = mapped_column(String(500), nullable=False)
    instagram_username: Mapped[str | None] = mapped_column(String(255))
    latest_instagram_followers: Mapped[int | None] = mapped_column(Integer)
    trustpilot_business_unit_id: Mapped[str | None] = mapped_column(String(255))
    trustpilot_matched_domain: Mapped[str | None] = mapped_column(String(500))
    trustpilot_last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    latest_trustpilot_review_count: Mapped[int | None] = mapped_column(Integer)
    latest_trustpilot_review_source: Mapped[str | None] = mapped_column(String(50))
    latest_trustpilot_trust_score: Mapped[float | None] = mapped_column(Numeric(3, 2))
    latest_trustpilot_stars: Mapped[float | None] = mapped_column(Numeric(2, 1))
    latest_trustpilot_profile_url: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    ads: Mapped[list["Ad"]] = relationship(
        back_populates="advertiser", cascade="all, delete-orphan"
    )
    observations: Mapped[list["AdvertiserObservation"]] = relationship(
        back_populates="advertiser", cascade="all, delete-orphan"
    )
    company: Mapped[Company] = relationship(back_populates="advertisers")


class Ad(Base):
    __tablename__ = "ads"
    __table_args__ = (UniqueConstraint("meta_ad_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meta_ad_id: Mapped[str] = mapped_column(String(255), nullable=False)
    advertiser_id: Mapped[int] = mapped_column(
        ForeignKey("advertisers.id", ondelete="CASCADE"), nullable=False
    )
    ad_start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ad_text: Mapped[str | None] = mapped_column(Text)
    snapshot_url: Mapped[str | None] = mapped_column(Text)
    landing_page_url: Mapped[str | None] = mapped_column(Text)
    landing_page_domain: Mapped[str | None] = mapped_column(String(500))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    advertiser: Mapped[Advertiser] = relationship(back_populates="ads")


class AdvertiserObservation(Base):
    __tablename__ = "advertiser_observations"
    __table_args__ = (
        UniqueConstraint("advertiser_id", "scan_run_id"),
        Index("ix_advertiser_observations_observed_at", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    advertiser_id: Mapped[int] = mapped_column(
        ForeignKey("advertisers.id", ondelete="CASCADE"), nullable=False
    )
    scan_run_id: Mapped[int] = mapped_column(
        ForeignKey("scan_runs.id", ondelete="CASCADE"), nullable=False
    )
    instagram_followers: Mapped[int | None] = mapped_column(Integer)
    active_ad_count: Mapped[int] = mapped_column(Integer, nullable=False)
    supplement_relevant: Mapped[bool | None] = mapped_column()
    relevance_reason: Mapped[str | None] = mapped_column(Text)
    spend_estimate_low_usd: Mapped[float | None] = mapped_column(Numeric(12, 2))
    spend_estimate_high_usd: Mapped[float | None] = mapped_column(Numeric(12, 2))
    spend_estimation_method: Mapped[str | None] = mapped_column(String(50))
    spend_estimation_source: Mapped[str | None] = mapped_column(String(50))
    spend_estimation_confidence: Mapped[str | None] = mapped_column(String(10))
    spend_estimation_inputs: Mapped[dict | None] = mapped_column(JSON)
    spend_estimation_assumptions: Mapped[dict | None] = mapped_column(JSON)
    spend_target_match: Mapped[bool | None] = mapped_column()
    review_source: Mapped[str | None] = mapped_column(String(50))
    review_count: Mapped[int | None] = mapped_column(Integer)
    review_trust_score: Mapped[float | None] = mapped_column(Numeric(3, 2))
    review_stars: Mapped[float | None] = mapped_column(Numeric(2, 1))
    review_business_unit_id: Mapped[str | None] = mapped_column(String(255))
    review_matched_domain: Mapped[str | None] = mapped_column(String(500))
    review_profile_url: Mapped[str | None] = mapped_column(Text)
    review_desirable: Mapped[bool | None] = mapped_column()
    review_status: Mapped[str | None] = mapped_column(String(20))
    review_reason: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    advertiser: Mapped[Advertiser] = relationship(back_populates="observations")
    scan_run: Mapped[ScanRun] = relationship(back_populates="observations")


class TrustpilotPaidLookup(Base):
    """One conservatively reserved paid Trustpilot domain lookup per UTC day."""

    __tablename__ = "trustpilot_paid_lookups"
    __table_args__ = (
        UniqueConstraint("lookup_date", "domain"),
        Index("ix_trustpilot_paid_lookups_lookup_date", "lookup_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lookup_date: Mapped[date] = mapped_column(Date, nullable=False)
    domain: Mapped[str] = mapped_column(String(500), nullable=False)
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class GoogleSheetRow(Base):
    """Cached row location; developer metadata is the authoritative identity."""

    __tablename__ = "google_sheet_rows"
    __table_args__ = (UniqueConstraint("company_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    spreadsheet_id: Mapped[str] = mapped_column(String(255), nullable=False)
    sheet_tab: Mapped[str] = mapped_column(String(100), nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    developer_metadata_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    last_exported_first_seen: Mapped[date] = mapped_column(Date, nullable=False)
    last_exported_brand: Mapped[str] = mapped_column(String(500), nullable=False)
    last_exported_region: Mapped[str | None] = mapped_column(String(500))
    last_exported_instagram: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    company: Mapped[Company] = relationship(back_populates="sheet_row")


class CompanyObservation(Base):
    __tablename__ = "company_observations"
    __table_args__ = (UniqueConstraint("company_id", "scan_run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False)
    scan_run_id: Mapped[int] = mapped_column(ForeignKey("scan_runs.id"), nullable=False)
    explicitly_disqualified: Mapped[bool | None] = mapped_column()
    disqualification_reasons: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AdvertiserCompanyMapping(Base):
    """Auditable history of conservative advertiser-to-company mappings."""

    __tablename__ = "advertiser_company_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    advertiser_id: Mapped[int] = mapped_column(ForeignKey("advertisers.id"), nullable=False)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False)
    scan_run_id: Mapped[int | None] = mapped_column(ForeignKey("scan_runs.id"))
    verified_domain: Mapped[str | None] = mapped_column(String(500))
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CompanyCandidateEvent(Base):
    __tablename__ = "company_candidate_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False)
    scan_run_id: Mapped[int | None] = mapped_column(ForeignKey("scan_runs.id"))
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
