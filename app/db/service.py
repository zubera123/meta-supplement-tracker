"""Transactional service for persisting complete Meta scan results."""

import logging
from collections.abc import Sequence

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.repository import ScanRepository
from app.db.session import (
    DatabaseConfigurationError,
    create_database_engine,
    create_session_factory,
)
from app.models import AdRecord


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

    def create_scan_run(self, regions: Sequence[str]) -> int:
        try:
            with self._session_factory.begin() as session:
                scan_run = ScanRepository(session).create_scan_run(list(regions))
                return scan_run.id
        except SQLAlchemyError as exc:
            raise DatabasePersistenceError("Could not create the scan-run record") from exc

    def persist_success(self, scan_run_id: int, records: Sequence[AdRecord]) -> None:
        try:
            with self._session_factory.begin() as session:
                repository = ScanRepository(session)
                for record in records:
                    advertiser = repository.upsert_advertiser(record)
                    for ad_details in record.ads:
                        repository.upsert_ad(
                            advertiser, ad_details, seen_at=record.observed_at
                        )
                    repository.write_advertiser_observation(
                        advertiser, scan_run_id, record
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

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
