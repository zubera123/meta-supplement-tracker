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

    observations: Mapped[list["AdvertiserObservation"]] = relationship(
        back_populates="scan_run", cascade="all, delete-orphan"
    )


class Advertiser(Base):
    __tablename__ = "advertisers"
    __table_args__ = (
        UniqueConstraint("meta_page_id"),
        Index("ix_advertisers_instagram_username", "instagram_username"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meta_page_id: Mapped[str | None] = mapped_column(String(255))
    page_name: Mapped[str] = mapped_column(String(500), nullable=False)
    instagram_username: Mapped[str | None] = mapped_column(String(255))
    latest_instagram_followers: Mapped[int | None] = mapped_column(Integer)
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
    sheet_row: Mapped["GoogleSheetRow | None"] = relationship(
        back_populates="advertiser", cascade="all, delete-orphan"
    )


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
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    advertiser: Mapped[Advertiser] = relationship(back_populates="observations")
    scan_run: Mapped[ScanRun] = relationship(back_populates="observations")


class GoogleSheetRow(Base):
    """PostgreSQL-only state used to update one visible row per advertiser."""

    __tablename__ = "google_sheet_rows"
    __table_args__ = (UniqueConstraint("advertiser_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    advertiser_id: Mapped[int] = mapped_column(
        ForeignKey("advertisers.id", ondelete="CASCADE"), nullable=False
    )
    spreadsheet_id: Mapped[str] = mapped_column(String(255), nullable=False)
    sheet_tab: Mapped[str] = mapped_column(String(100), nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
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

    advertiser: Mapped[Advertiser] = relationship(back_populates="sheet_row")
