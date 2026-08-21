"""Canonical-company grouping and conservative candidate lifecycle tests."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Advertiser, Company
from app.db.service import ScanPersistenceService
from app.models import AdRecord, Brand, MetaAdDetails, Region, RelevanceResult, SocialStats


def factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def record(page: str, *, domain: str | None, followers: int | None = 20_000,
           when: datetime | None = None, region: Region = Region.UK) -> AdRecord:
    when = when or datetime(2026, 8, 1, tzinfo=UTC)
    return AdRecord(
        brand=Brand(name=f"Brand {page}", source_id=page), region=region,
        ads=[MetaAdDetails(ad_id=f"ad-{page}-{when.day}", page_id=page,
            page_name=f"Brand {page}", landing_page_domain=domain)],
        social_stats=(SocialStats(instagram_handle=f"ig_{page}",
            instagram_followers=followers, observed_at=when) if followers is not None else None),
        observed_at=when,
    )


RELEVANT = RelevanceResult(
    is_relevant=True, has_positive_evidence=True, reason="supplement"
)
AMBIGUOUS = RelevanceResult(
    is_relevant=True,
    has_positive_evidence=False,
    reason="ambiguous: no positive supplement evidence",
)


def persist(service: ScanPersistenceService, items: list[AdRecord], decisions=None,
            *, complete: bool = True, region: str = "UK") -> int:
    run = service.create_scan_run([region])
    service.persist_success(run, items, decisions, coverage_complete=complete,
                            absent_days=1, scan_interval_hours=12)
    return run


def test_exact_registrable_domain_groups_pages_but_preserves_advertisers() -> None:
    sessions = factory()
    service = ScanPersistenceService(sessions)
    first = record("one", domain="www.shop.example.co.uk")
    second = record("two", domain="https://offers.example.co.uk/path?utm=x")
    persist(service, [first, second], [RELEVANT, RELEVANT])
    candidates, _ = service.prepare_sheet_candidates(
        [first, second], minimum_followers=10_000, maximum_followers=100_000,
        relevance_results=[RELEVANT, RELEVANT],
    )
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Advertiser)) == 2
        assert session.scalar(select(func.count()).select_from(Company)) == 1
        assert session.scalar(select(Company.canonical_domain)) == "example.co.uk"
    assert len(candidates) == 1
    assert candidates[0].active_ads == 2


def test_conflicting_or_missing_domains_never_merge_by_name() -> None:
    sessions = factory()
    service = ScanPersistenceService(sessions)
    items = [record("one", domain="one.com"), record("two", domain="two.com"),
             record("three", domain=None), record("four", domain=None)]
    persist(service, items)
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Company)) == 4


def test_ambiguous_evidence_uses_three_complete_scan_lifecycle() -> None:
    sessions = factory()
    service = ScanPersistenceService(sessions)
    base = datetime(2026, 8, 1, tzinfo=UTC)
    persist(
        service,
        [record("lifecycle", domain="lifecycle.example", when=base)],
        [RELEVANT],
    )

    for day in (2, 3):
        item = record(
            "lifecycle", domain="lifecycle.example", when=base + timedelta(days=day)
        )
        persist(service, [item], [AMBIGUOUS], complete=True)
        with sessions() as session:
            assert session.scalar(select(Company.sheet_eligible)) is True

    final = record(
        "lifecycle", domain="lifecycle.example", when=base + timedelta(days=4)
    )
    persist(service, [final], [AMBIGUOUS], complete=True)

    with sessions() as session:
        company = session.scalar(select(Company))
        assert company is not None
        assert company.sheet_eligible is False
        assert company.consecutive_disqualifications == 3


def test_ambiguous_new_company_is_persisted_but_not_sheet_eligible() -> None:
    sessions = factory()
    service = ScanPersistenceService(sessions)
    item = record("ambiguous", domain="ambiguous.example")

    persist(service, [item], [AMBIGUOUS])

    with sessions() as session:
        company = session.scalar(select(Company))
        assert company is not None
        assert company.sheet_eligible is False


def test_missing_observation_breaks_consecutive_explicit_failures() -> None:
    sessions = factory()
    service = ScanPersistenceService(sessions)
    start = datetime(2026, 8, 1, tzinfo=UTC)
    persist(service, [record("one", domain="example.com", when=start)], [RELEVANT])
    for offset, followers in enumerate((9_000, None, 9_000, 9_000), start=1):
        item = record("one", domain="example.com", followers=followers,
                      when=start + timedelta(hours=12 * offset))
        persist(service, [item], [RELEVANT])
    with sessions() as session:
        company = session.scalar(select(Company))
        assert company is not None
        assert company.consecutive_disqualifications == 2
        assert company.sheet_eligible is True
    final = record("one", domain="example.com", followers=9_000,
                   when=start + timedelta(hours=60))
    persist(service, [final], [RELEVANT])
    with sessions() as session:
        assert session.scalar(select(Company.sheet_eligible)) is False


def test_capped_scans_and_unrelated_regions_do_not_age_absence() -> None:
    sessions = factory()
    service = ScanPersistenceService(sessions)
    persist(service, [record("one", domain="example.com")], [RELEVANT])
    persist(service, [], complete=False)
    persist(service, [], region="USA")
    with sessions() as session:
        assert session.scalar(select(Company.consecutive_absent_successful_scans)) == 0


def test_absence_requires_complete_scans_and_requalification_keeps_first_seen() -> None:
    sessions = factory()
    service = ScanPersistenceService(sessions)
    original = record("one", domain="example.com")
    persist(service, [original], [RELEVANT])
    persist(service, [])
    persist(service, [])
    with sessions() as session:
        company = session.scalar(select(Company))
        assert company is not None and company.sheet_eligible is False
        first_seen = company.first_seen_at
    returned = record("one", domain="example.com", when=datetime(2026, 9, 2, tzinfo=UTC))
    persist(service, [returned], [RELEVANT])
    with sessions() as session:
        company = session.scalar(select(Company))
        assert company is not None and company.sheet_eligible is True
        assert company.first_seen_at == first_seen


def test_failed_scan_does_not_advance_disqualification_counter() -> None:
    sessions = factory()
    service = ScanPersistenceService(sessions)
    start = datetime(2026, 8, 1, tzinfo=UTC)
    persist(service, [record("one", domain="example.com", when=start)], [RELEVANT])
    for offset in (1, 2):
        bad = record("one", domain="example.com", followers=9_000,
                     when=start + timedelta(hours=12 * offset))
        persist(service, [bad], [RELEVANT])
    failed = record("one", domain="example.com", followers=9_000,
                    when=start + timedelta(hours=36))
    run = persist(service, [failed], [RELEVANT])
    service.record_failure(run, RuntimeError("Sheet output failed"))

    with sessions() as session:
        company = session.scalar(select(Company))
        assert company is not None
        assert company.consecutive_disqualifications == 2
        assert company.sheet_eligible is True
