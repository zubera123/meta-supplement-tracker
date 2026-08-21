"""Transactional service for persisting complete Meta scan results."""

import logging
from datetime import UTC, datetime
from collections.abc import Sequence

from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.locks import ScanExecutionLock, ScanLockError, try_acquire_scan_lock
from app.db.repository import ScanRepository
from app.db.session import (
    DatabaseConfigurationError,
    create_database_engine,
    create_session_factory,
)
from app.db.models import (
    Ad, Advertiser, AdvertiserObservation, Company, CompanyCandidateEvent,
    CompanyObservation, GoogleSheetRow, ScanRun, utc_now,
    TrustpilotPaidLookup,
)
from app.models import (
    AdRecord,
    Brand,
    MetaAdDetails,
    Region,
    RelevanceResult,
    ReviewCache,
    SheetCandidate,
    SheetRowState,
    SpendHistory,
)
from app.services.spend_estimation import format_spend_range


logger = logging.getLogger(__name__)


class DatabasePersistenceError(RuntimeError):
    """Raised when a persistence transaction cannot be completed."""


class DatabaseUnavailableError(DatabasePersistenceError):
    """Raised when the configured database cannot be reached."""


class ScanPersistenceService:
    """Own transaction boundaries for one scan's durable state."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        engine: Engine | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._engine = engine

    @classmethod
    def from_database_url(
        cls,
        database_url: str | None,
        *,
        connect_timeout_seconds: int = 10,
    ) -> "ScanPersistenceService":
        engine = create_database_engine(
            database_url, connect_timeout_seconds=connect_timeout_seconds
        )
        return cls(create_session_factory(engine), engine=engine)

    def verify_connection(self) -> None:
        if self._engine is None:
            return
        try:
            with self._engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise DatabaseUnavailableError(
                "Database connection failed; verify DATABASE_URL, the Railway "
                "reference variable, and PostgreSQL service health"
            ) from exc

    def try_acquire_scan_lock(self) -> ScanExecutionLock | None:
        """Try to exclude every other scheduled or manual full scan."""

        if self._engine is None:
            raise ScanLockError(
                "Candidate scan overlap protection requires a database engine"
            )
        return try_acquire_scan_lock(self._engine)

    def create_scan_run(self, regions: Sequence[str]) -> int:
        try:
            with self._session_factory.begin() as session:
                scan_run = ScanRepository(session).create_scan_run(list(regions))
                return scan_run.id
        except SQLAlchemyError as exc:
            raise DatabasePersistenceError("Could not create the scan-run record") from exc

    def persist_success(
        self,
        scan_run_id: int,
        records: Sequence[AdRecord],
        relevance_results: Sequence[RelevanceResult] | None = None,
        minimum_followers: int = 10_000,
        maximum_followers: int = 100_000,
        disqualify_scans: int = 3,
        absent_days: int = 30,
        scan_interval_hours: int = 12,
        coverage_complete: bool = True,
    ) -> None:
        if relevance_results is not None and len(relevance_results) != len(records):
            raise DatabasePersistenceError(
                "Relevance decisions do not match the discovered advertiser count"
            )
        try:
            with self._session_factory.begin() as session:
                repository = ScanRepository(session)
                companies: dict[int, list[tuple[AdRecord, RelevanceResult | None]]] = {}
                for index, record in enumerate(records):
                    relevance = (
                        relevance_results[index]
                        if relevance_results is not None
                        else None
                    )
                    advertiser = repository.upsert_advertiser(
                        record, scan_run_id=scan_run_id
                    )
                    companies.setdefault(advertiser.company_id, []).append((record, relevance))
                    for ad_details in record.ads:
                        repository.upsert_ad(
                            advertiser, ad_details, seen_at=record.observed_at
                        )
                    repository.write_advertiser_observation(
                        advertiser, scan_run_id, record, relevance
                    )
                now = max((record.observed_at for record in records), default=utc_now())
                for company_id, items in companies.items():
                    company = session.get(Company, company_id)
                    if company is None:
                        continue
                    observed_first = min(r.observed_at for r, _ in items)
                    stored_first = company.first_seen_at
                    if stored_first.tzinfo is None:
                        stored_first = stored_first.replace(tzinfo=UTC)
                    company.first_seen_at = min(stored_first, observed_first)
                    company.last_seen_at = max(r.observed_at for r, _ in items)
                    company.regions = sorted({
                        region.value for record, _ in items
                        for region in (record.regions or [record.region])
                    })
                    company.consecutive_absent_successful_scans = 0
                    reasons: list[str] = []
                    relevances = [decision.is_relevant for _, decision in items if decision]
                    positive_evidence = [
                        decision.has_positive_evidence
                        for _, decision in items
                        if decision
                    ]
                    if relevances and not any(relevances):
                        reasons.append("supplement relevance is false")
                    elif positive_evidence and not any(positive_evidence):
                        reasons.append("no positive supplement evidence")
                    followers = [
                        r.social_stats.instagram_followers for r, _ in items
                        if r.social_stats and r.social_stats.instagram_followers is not None
                    ]
                    if followers and not any(minimum_followers <= f <= maximum_followers for f in followers):
                        reasons.append("Instagram followers are outside the target range")
                    reliable_spend = [
                        r.spend_estimate.target_match for r, _ in items
                        if r.spend_estimate and r.spend_estimate.method in {"impressions_cpm", "reach_cpm"}
                    ]
                    if reliable_spend and not any(value is True for value in reliable_spend):
                        reasons.append("reliable spend estimate is outside the target range")
                    explicit = bool(reasons)
                    qualifies = (
                        (not positive_evidence or any(positive_evidence))
                        and any(minimum_followers <= f <= maximum_followers for f in followers)
                        and not (reliable_spend and not any(value is True for value in reliable_spend))
                    )
                    session.add(CompanyObservation(
                        company_id=company_id, scan_run_id=scan_run_id,
                        explicitly_disqualified=explicit if reasons else None,
                        disqualification_reasons=reasons, observed_at=now,
                    ))
                    if coverage_complete:
                        company.consecutive_disqualifications = (
                            company.consecutive_disqualifications + 1 if explicit else 0
                        )
                    if qualifies:
                        if not company.sheet_eligible:
                            session.add(CompanyCandidateEvent(
                                company_id=company.id, scan_run_id=scan_run_id,
                                event_type="qualified", reason="current evidence qualifies",
                                occurred_at=now,
                            ))
                        company.sheet_eligible = True
                    if company.consecutive_disqualifications >= disqualify_scans:
                        company.sheet_eligible = False
                        session.add(CompanyCandidateEvent(
                            company_id=company.id, scan_run_id=scan_run_id,
                            event_type="disqualified", reason="; ".join(reasons), occurred_at=now,
                        ))
                if coverage_complete:
                    absent_limit = max(1, (absent_days * 24 + scan_interval_hours - 1) // scan_interval_hours)
                    scanned_regions = set(repository._scan_run(scan_run_id).regions)
                    unseen = session.scalars(
                        select(Company).where(Company.merged_into_company_id.is_(None))
                        .where(Company.id.not_in(companies))
                    ).all()
                    for company in unseen:
                        if not scanned_regions.intersection(company.regions):
                            continue
                        company.consecutive_absent_successful_scans += 1
                        if company.consecutive_absent_successful_scans >= absent_limit and company.sheet_eligible:
                            company.sheet_eligible = False
                            session.add(CompanyCandidateEvent(
                                company_id=company.id, scan_run_id=scan_run_id,
                                event_type="absent", reason=f"absent from {absent_limit} successful complete scans",
                                occurred_at=now,
                            ))
                repository.complete_scan_run(
                    scan_run_id,
                    ads_found=sum(len(record.ads) for record in records),
                    advertisers_found=len(records),
                )
                repository._scan_run(scan_run_id).coverage_complete = coverage_complete
        except SQLAlchemyError as exc:
            raise DatabasePersistenceError("Could not persist scan results") from exc

    def record_failure(self, scan_run_id: int, error: BaseException) -> None:
        message = f"{type(error).__name__}: {error}"
        try:
            with self._session_factory.begin() as session:
                repository = ScanRepository(session)
                existing_run = session.get(ScanRun, scan_run_id)
                was_succeeded = bool(existing_run and existing_run.status == "succeeded")
                run = repository.fail_scan_run(scan_run_id, message)
                observations = list(session.scalars(
                    select(CompanyObservation).where(
                        CompanyObservation.scan_run_id == scan_run_id
                    )
                ))
                observed_ids = {item.company_id for item in observations}
                events = list(session.scalars(
                    select(CompanyCandidateEvent).where(
                        CompanyCandidateEvent.scan_run_id == scan_run_id
                    )
                ))
                event_company_ids = {item.company_id for item in events}
                affected_ids = observed_ids | event_company_ids
                for event in events:
                    session.delete(event)
                session.flush()
                if was_succeeded and run.coverage_complete:
                    for company in session.scalars(
                        select(Company).where(Company.merged_into_company_id.is_(None))
                    ):
                        if company.id not in observed_ids and set(run.regions).intersection(company.regions):
                            company.consecutive_absent_successful_scans = max(
                                0, company.consecutive_absent_successful_scans - 1
                            )
                            affected_ids.add(company.id)
                for company_id in affected_ids:
                    company = session.get(Company, company_id)
                    if company is None:
                        continue
                    prior = list(session.scalars(
                        select(CompanyObservation)
                        .join(ScanRun, ScanRun.id == CompanyObservation.scan_run_id)
                        .where(CompanyObservation.company_id == company_id)
                        .where(ScanRun.status == "succeeded")
                        .where(ScanRun.coverage_complete.is_(True))
                        .order_by(CompanyObservation.observed_at.desc())
                    ))
                    failures = 0
                    for observation in prior:
                        if observation.explicitly_disqualified is True:
                            failures += 1
                        else:
                            break
                    company.consecutive_disqualifications = failures
                    if company_id in event_company_ids:
                        latest_event = session.scalar(
                            select(CompanyCandidateEvent)
                            .where(CompanyCandidateEvent.company_id == company_id)
                            .order_by(CompanyCandidateEvent.occurred_at.desc())
                            .limit(1)
                        )
                        company.sheet_eligible = bool(
                            latest_event and latest_event.event_type == "qualified"
                        )
        except SQLAlchemyError as exc:
            logger.exception("Could not record failed scan run", extra={"scan_run_id": scan_run_id})
            raise DatabasePersistenceError(
                "The scan failed and its failure record could not be written"
            ) from exc

    def prepare_sheet_candidates(
        self,
        records: Sequence[AdRecord],
        *,
        minimum_followers: int,
        maximum_followers: int,
        relevance_results: Sequence[RelevanceResult] | None = None,
    ) -> tuple[list[SheetCandidate], dict[int, SheetRowState]]:
        """Load stable IDs and original first-seen dates from PostgreSQL."""

        if relevance_results is not None and len(relevance_results) != len(records):
            raise DatabasePersistenceError(
                "Relevance decisions do not match the discovered advertiser count"
            )
        try:
            with self._session_factory() as session:
                repository = ScanRepository(session)
                grouped: dict[int, list[AdRecord]] = {}
                for index, record in enumerate(records):
                    advertiser = repository.find_advertiser(record)
                    if advertiser is None:
                        raise DatabasePersistenceError(
                            "A discovered advertiser was not found after persistence"
                        )
                    grouped.setdefault(advertiser.company_id, []).append(record)
                candidates: list[SheetCandidate] = []
                for company_id, company_records in grouped.items():
                    company = session.get(Company, company_id)
                    if company is None or not company.sheet_eligible:
                        continue
                    valid = [r for r in company_records if r.social_stats and r.social_stats.instagram_followers is not None and minimum_followers <= r.social_stats.instagram_followers <= maximum_followers]
                    if not valid:
                        continue
                    representative = max(valid, key=lambda r: r.observed_at)
                    advertiser = repository.find_advertiser(representative)
                    assert advertiser is not None
                    followers = representative.social_stats.instagram_followers
                    unique_ads = {ad.ad_id for r in company_records for ad in r.ads}
                    spend_record = max(
                        company_records,
                        key=lambda r: (
                            1 if r.spend_estimate and r.spend_estimate.target_match is True else 0,
                            {"high": 3, "medium": 2, "low": 1, "very_low": 0}.get(
                                r.spend_estimate.confidence, -1
                            ) if r.spend_estimate else -1,
                        ),
                    )
                    review_record = next((r for r in sorted(company_records, key=lambda r: r.observed_at, reverse=True) if r.review_enrichment and r.review_enrichment.stats), representative)
                    candidates.append(
                        SheetCandidate(
                            company_id=company.id,
                            first_seen=company.first_seen_at.date(),
                            brand=advertiser.page_name,
                            region=", ".join(company.regions),
                            instagram_username=advertiser.instagram_username,
                            followers=followers,
                            active_ads=(
                                len(unique_ads)
                            ),
                            spend_estimate=format_spend_range(spend_record.spend_estimate),
                            spend_source=(
                                spend_record.spend_estimate.source
                                if spend_record.spend_estimate is not None
                                else "Unknown"
                            ),
                            review_count=(
                                review_record.review_enrichment.stats.review_count
                                if review_record.review_enrichment
                                and review_record.review_enrichment.stats
                                else None
                            ),
                            review_source=(
                                review_record.review_enrichment.stats.source
                                if review_record.review_enrichment
                                and review_record.review_enrichment.stats
                                else None
                            ),
                        )
                    )
                states = repository.sheet_row_states(
                    [candidate.company_id for candidate in candidates]
                )
                return candidates, states
        except SQLAlchemyError as exc:
            raise DatabasePersistenceError(
                "Could not prepare candidates for Google Sheets"
            ) from exc

    def load_review_caches(self, records: Sequence[AdRecord]) -> list[ReviewCache]:
        """Load persisted Trustpilot IDs and refresh timestamps in record order."""

        try:
            with self._session_factory() as session:
                repository = ScanRepository(session)
                return [repository.review_cache(record) for record in records]
        except SQLAlchemyError as exc:
            raise DatabasePersistenceError(
                "Could not load Trustpilot review cache"
            ) from exc

    def reserve_trustpilot_paid_lookup(
        self,
        domain: str,
        daily_limit: int,
        *,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        """Atomically reserve one unique paid domain lookup for the UTC day."""

        observed_at = now or datetime.now(UTC)
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        lookup_date = observed_at.astimezone(UTC).date()
        try:
            with self._session_factory.begin() as session:
                if session.bind is not None and session.bind.dialect.name == "postgresql":
                    # Serialize the count-and-insert decision across web/Cron processes.
                    session.execute(
                        text("SELECT pg_advisory_xact_lock(:lock_key)"),
                        {"lock_key": 7_321_984_231_104_077},
                    )
                existing = session.scalar(
                    select(TrustpilotPaidLookup.id)
                    .where(TrustpilotPaidLookup.lookup_date == lookup_date)
                    .where(TrustpilotPaidLookup.domain == domain)
                )
                if existing is not None:
                    return (
                        False,
                        "Trustpilot lookup deferred: this domain already had a paid "
                        "lookup reserved today (UTC)",
                    )
                used = session.scalar(
                    select(func.count())
                    .select_from(TrustpilotPaidLookup)
                    .where(TrustpilotPaidLookup.lookup_date == lookup_date)
                ) or 0
                if used >= daily_limit:
                    return (
                        False,
                        "Trustpilot lookup deferred: UTC daily unique paid lookup "
                        f"limit of {daily_limit} is exhausted",
                    )
                session.add(
                    TrustpilotPaidLookup(
                        lookup_date=lookup_date,
                        domain=domain,
                        reserved_at=observed_at,
                    )
                )
                return True, "Trustpilot paid lookup reserved"
        except SQLAlchemyError as exc:
            raise DatabasePersistenceError(
                "Could not enforce the Trustpilot daily paid-lookup limit"
            ) from exc

    def load_spend_histories(
        self, records: Sequence[AdRecord]
    ) -> list[SpendHistory]:
        """Load prior observations in record order without mutating the database."""

        try:
            with self._session_factory() as session:
                repository = ScanRepository(session)
                histories: list[SpendHistory] = []
                for record in records:
                    advertiser = repository.find_advertiser(record)
                    if advertiser is None:
                        histories.append(SpendHistory())
                        continue
                    counts = list(
                        session.scalars(
                            select(AdvertiserObservation.active_ad_count)
                            .where(AdvertiserObservation.advertiser_id == advertiser.id)
                            .order_by(AdvertiserObservation.observed_at)
                        )
                    )
                    histories.append(
                        SpendHistory(
                            observation_count=len(counts),
                            active_ad_counts=counts,
                        )
                    )
                return histories
        except SQLAlchemyError as exc:
            raise DatabasePersistenceError(
                "Could not load advertiser activity history"
            ) from exc

    def load_spend_dry_run_records(
        self,
    ) -> tuple[list[AdRecord], list[SpendHistory]]:
        """Reconstruct only observed DB activity for a no-provider estimate preview."""

        try:
            with self._session_factory() as session:
                advertisers = list(session.scalars(select(Advertiser)))
                records: list[AdRecord] = []
                histories: list[SpendHistory] = []
                for advertiser in advertisers:
                    ads = list(
                        session.scalars(
                            select(Ad).where(Ad.advertiser_id == advertiser.id)
                        )
                    )
                    observations = list(
                        session.scalars(
                            select(AdvertiserObservation)
                            .where(AdvertiserObservation.advertiser_id == advertiser.id)
                            .order_by(AdvertiserObservation.observed_at)
                        )
                    )
                    if not observations:
                        continue
                    sheet_row = session.scalar(
                        select(GoogleSheetRow).where(
                            GoogleSheetRow.company_id == advertiser.company_id
                        )
                    )
                    regions = _stored_regions(
                        sheet_row.last_exported_region if sheet_row else None
                    )
                    records.append(
                        AdRecord(
                            brand=Brand(
                                name=advertiser.page_name,
                                source_id=advertiser.meta_page_id,
                                instagram_handle=advertiser.instagram_username,
                            ),
                            region=regions[0],
                            regions=regions,
                            active_ad_count=observations[-1].active_ad_count,
                            ads=[
                                MetaAdDetails(
                                    ad_id=ad.meta_ad_id,
                                    page_id=advertiser.meta_page_id or f"db-{advertiser.id}",
                                    page_name=advertiser.page_name,
                                    ad_delivery_start_time=ad.ad_start_date,
                                    ad_snapshot_url=ad.snapshot_url,
                                    landing_page_url=ad.landing_page_url,
                                    landing_page_domain=ad.landing_page_domain,
                                    creative_bodies=[ad.ad_text] if ad.ad_text else [],
                                )
                                for ad in ads
                            ],
                            observed_at=observations[-1].observed_at,
                        )
                    )
                    histories.append(
                        SpendHistory(
                            observation_count=len(observations),
                            active_ad_counts=[item.active_ad_count for item in observations],
                        )
                    )
                return records, histories
        except SQLAlchemyError as exc:
            raise DatabasePersistenceError(
                "Could not load existing advertiser data for spend dry-run"
            ) from exc

    def save_sheet_row_states(self, states: Sequence[SheetRowState]) -> None:
        try:
            with self._session_factory.begin() as session:
                repository = ScanRepository(session)
                for state in states:
                    repository.upsert_sheet_row_state(state)
        except SQLAlchemyError as exc:
            raise DatabasePersistenceError(
                "Google Sheet was updated but its PostgreSQL row mappings could not be saved"
            ) from exc

    def load_all_sheet_row_states(
        self, spreadsheet_id: str, sheet_tab: str
    ) -> dict[int, SheetRowState]:
        try:
            with self._session_factory() as session:
                return ScanRepository(session).all_sheet_row_states(
                    spreadsheet_id, sheet_tab
                )
        except SQLAlchemyError as exc:
            raise DatabasePersistenceError(
                "Could not load managed Google Sheet row mappings"
            ) from exc

    def sheet_rows_to_remove(self) -> dict[int, SheetRowState]:
        """Return mapped companies that are explicitly stale or merged."""
        try:
            with self._session_factory() as session:
                ids = list(session.scalars(
                    select(Company.id).where(
                        (Company.sheet_eligible.is_(False))
                        | (Company.merged_into_company_id.is_not(None))
                    )
                ))
                return ScanRepository(session).sheet_row_states(ids)
        except SQLAlchemyError as exc:
            raise DatabasePersistenceError("Could not load stale Sheet mappings") from exc

    def delete_sheet_row_states(self, company_ids: Sequence[int]) -> None:
        if not company_ids:
            return
        try:
            with self._session_factory.begin() as session:
                rows = session.scalars(
                    select(GoogleSheetRow).where(GoogleSheetRow.company_id.in_(company_ids))
                ).all()
                for row in rows:
                    session.delete(row)
        except SQLAlchemyError as exc:
            raise DatabasePersistenceError("Could not remove stale Sheet mappings") from exc

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()


def _readable_region(record: AdRecord) -> str:
    regions = record.regions or [record.region]
    return ", ".join(dict.fromkeys(region.value for region in regions))


def _stored_regions(value: str | None) -> list[Region]:
    if value:
        parsed = [
            region
            for item in value.split(",")
            if (region := next((r for r in Region if r.value == item.strip()), None))
        ]
        if parsed:
            return parsed
    # Region was not persisted on historical advertiser/ad rows. Use the full
    # configured region envelope rather than guessing a specific location.
    return [Region.EUROPE, Region.UK, Region.USA, Region.CANADA]
