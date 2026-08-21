"""Transactional service for persisting complete Meta scan results."""

import logging
from collections.abc import Sequence

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.locks import ScanExecutionLock, ScanLockError, try_acquire_scan_lock
from app.db.repository import ScanRepository
from app.db.session import (
    DatabaseConfigurationError,
    create_database_engine,
    create_session_factory,
)
from app.models import AdRecord, RelevanceResult, SheetCandidate, SheetRowState


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
    ) -> None:
        if relevance_results is not None and len(relevance_results) != len(records):
            raise DatabasePersistenceError(
                "Relevance decisions do not match the discovered advertiser count"
            )
        try:
            with self._session_factory.begin() as session:
                repository = ScanRepository(session)
                for index, record in enumerate(records):
                    relevance = (
                        relevance_results[index]
                        if relevance_results is not None
                        else None
                    )
                    advertiser = repository.upsert_advertiser(record)
                    for ad_details in record.ads:
                        repository.upsert_ad(
                            advertiser, ad_details, seen_at=record.observed_at
                        )
                    repository.write_advertiser_observation(
                        advertiser, scan_run_id, record, relevance
                    )
                repository.complete_scan_run(
                    scan_run_id,
                    ads_found=sum(len(record.ads) for record in records),
                    advertisers_found=len(records),
                )
        except SQLAlchemyError as exc:
            raise DatabasePersistenceError("Could not persist scan results") from exc

    def record_failure(self, scan_run_id: int, error: BaseException) -> None:
        message = f"{type(error).__name__}: {error}"
        try:
            with self._session_factory.begin() as session:
                ScanRepository(session).fail_scan_run(scan_run_id, message)
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
                candidates: list[SheetCandidate] = []
                for index, record in enumerate(records):
                    if (
                        relevance_results is not None
                        and not relevance_results[index].is_relevant
                    ):
                        continue
                    followers = (
                        record.social_stats.instagram_followers
                        if record.social_stats is not None
                        else None
                    )
                    if (
                        followers is None
                        or followers < minimum_followers
                        or followers > maximum_followers
                    ):
                        continue
                    advertiser = repository.find_advertiser(record)
                    if advertiser is None:
                        raise DatabasePersistenceError(
                            "A discovered advertiser was not found after persistence"
                        )
                    candidates.append(
                        SheetCandidate(
                            advertiser_id=advertiser.id,
                            first_seen=advertiser.first_seen_at.date(),
                            brand=advertiser.page_name,
                            region=_readable_region(record),
                            instagram_username=advertiser.instagram_username,
                            followers=followers,
                            active_ads=(
                                record.active_ad_count
                                if record.active_ad_count is not None
                                else len(record.ads)
                            ),
                        )
                    )
                states = repository.sheet_row_states(
                    [candidate.advertiser_id for candidate in candidates]
                )
                return candidates, states
        except SQLAlchemyError as exc:
            raise DatabasePersistenceError(
                "Could not prepare candidates for Google Sheets"
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

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()


def _readable_region(record: AdRecord) -> str:
    regions = record.regions or [record.region]
    return ", ".join(dict.fromkeys(region.value for region in regions))
