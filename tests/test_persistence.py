"""Persistence tests using SQLite for SQL-compatible repository behavior."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db.base import Base
from app.db.models import Ad, Advertiser, AdvertiserObservation, ScanRun
from app.db.service import ScanPersistenceService
from app.db.session import DatabaseConfigurationError, normalize_database_url
from app.jobs.brand_scan import _run_meta_only
from app.models import AdRecord, Brand, MetaAdDetails, Region, SocialStats


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def ad_record(
    *,
    observed_at: datetime,
    followers: int | None,
    active_ad_count: int = 1,
    ad_text: str = "Magnesium gummies",
) -> AdRecord:
    social = SocialStats(
        instagram_handle="example_supplements",
        instagram_followers=followers,
        observed_at=observed_at,
    )
    return AdRecord(
        brand=Brand(
            name="Example Supplements",
            source_id="page-1",
            instagram_handle="example_supplements",
        ),
        region=Region.UK,
        regions=[Region.UK],
        active_ad_count=active_ad_count,
        ads=[
            MetaAdDetails(
                ad_id="ad-1",
                page_id="page-1",
                page_name="Example Supplements",
                ad_delivery_start_time=observed_at - timedelta(days=7),
                ad_snapshot_url="https://www.facebook.com/ads/library/?id=ad-1",
                creative_bodies=[ad_text],
                matched_regions=[Region.UK],
            )
        ],
        social_stats=social,
        observed_at=observed_at,
    )


def test_railway_postgres_url_uses_psycopg_3_driver() -> None:
    normalized = normalize_database_url(
        "postgres://postgres:encoded%40password@postgres.railway.internal:5432/railway"
    )

    assert normalized.drivername == "postgresql+psycopg"
    assert normalized.host == "postgres.railway.internal"
    assert normalized.database == "railway"


def test_missing_database_url_is_rejected() -> None:
    with pytest.raises(DatabaseConfigurationError, match="DATABASE_URL is required"):
        normalize_database_url(None)


def test_sqlite_runtime_url_is_rejected_for_production_configuration() -> None:
    with pytest.raises(DatabaseConfigurationError, match="Railway PostgreSQL"):
        normalize_database_url("sqlite:///tracker.db")


def test_repeated_scans_deduplicate_and_record_follower_history(
    session_factory: sessionmaker[Session],
) -> None:
    service = ScanPersistenceService(session_factory)
    first_seen = datetime(2026, 8, 20, 8, tzinfo=UTC)
    second_seen = first_seen + timedelta(hours=12)

    first_run_id = service.create_scan_run(["UK"])
    service.persist_success(
        first_run_id,
        [ad_record(observed_at=first_seen, followers=20_000)],
    )
    second_run_id = service.create_scan_run(["UK"])
    service.persist_success(
        second_run_id,
        [
            ad_record(
                observed_at=second_seen,
                followers=25_000,
                active_ad_count=2,
                ad_text="Updated magnesium gummies",
            )
        ],
    )

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Advertiser)) == 1
        assert session.scalar(select(func.count()).select_from(Ad)) == 1
        assert (
            session.scalar(select(func.count()).select_from(AdvertiserObservation))
            == 2
        )
        advertiser = session.scalar(select(Advertiser))
        assert advertiser is not None
        assert advertiser.latest_instagram_followers == 25_000
        assert advertiser.first_seen_at == first_seen.replace(tzinfo=None)
        assert advertiser.last_seen_at == second_seen.replace(tzinfo=None)
        stored_ad = session.scalar(select(Ad))
        assert stored_ad is not None
        assert stored_ad.ad_text == "Updated magnesium gummies"
        observations = session.scalars(
            select(AdvertiserObservation).order_by(AdvertiserObservation.observed_at)
        ).all()
        assert [item.instagram_followers for item in observations] == [20_000, 25_000]
        assert [item.active_ad_count for item in observations] == [1, 2]


def test_unknown_follower_count_remains_null_in_latest_and_history(
    session_factory: sessionmaker[Session],
) -> None:
    service = ScanPersistenceService(session_factory)
    scan_run_id = service.create_scan_run(["UK"])
    service.persist_success(
        scan_run_id,
        [ad_record(observed_at=datetime(2026, 8, 20, tzinfo=UTC), followers=None)],
    )

    with session_factory() as session:
        advertiser = session.scalar(select(Advertiser))
        observation = session.scalar(select(AdvertiserObservation))
        assert advertiser is not None
        assert observation is not None
        assert advertiser.latest_instagram_followers is None
        assert observation.instagram_followers is None


def test_successful_scan_run_records_counts(
    session_factory: sessionmaker[Session],
) -> None:
    service = ScanPersistenceService(session_factory)
    scan_run_id = service.create_scan_run(["UK"])
    service.persist_success(
        scan_run_id,
        [ad_record(observed_at=datetime(2026, 8, 20, tzinfo=UTC), followers=30_000)],
    )

    with session_factory() as session:
        scan_run = session.get(ScanRun, scan_run_id)
        assert scan_run is not None
        assert scan_run.status == "succeeded"
        assert scan_run.finished_at is not None
        assert scan_run.ads_found == 1
        assert scan_run.advertisers_found == 1
        assert scan_run.error_message is None


def test_failed_scan_run_records_error(
    session_factory: sessionmaker[Session],
) -> None:
    service = ScanPersistenceService(session_factory)
    scan_run_id = service.create_scan_run(["UK", "Canada"])

    service.record_failure(scan_run_id, RuntimeError("provider unavailable"))

    with session_factory() as session:
        scan_run = session.get(ScanRun, scan_run_id)
        assert scan_run is not None
        assert scan_run.status == "failed"
        assert scan_run.finished_at is not None
        assert scan_run.regions == ["UK", "Canada"]
        assert scan_run.error_message == "RuntimeError: provider unavailable"


def test_enabled_persistence_requires_database_before_provider_configuration() -> None:
    settings = Settings(
        _env_file=None,
        persist_scan_results=True,
        database_url=None,
        meta_ad_provider="apify",
        apify_api_token=None,
    )

    with pytest.raises(DatabaseConfigurationError, match="DATABASE_URL is required"):
        asyncio.run(_run_meta_only(settings))
