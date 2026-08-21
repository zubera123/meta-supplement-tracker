"""PostgreSQL-backed mutual exclusion for paid candidate scans."""

import logging
from dataclasses import dataclass

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import SQLAlchemyError


logger = logging.getLogger(__name__)

# Stable, application-owned signed bigint key (ASCII "MSTSCAN"). Advisory lock
# keys are scoped to a PostgreSQL database, so every scanner using DATABASE_URL
# contends for this same lock without requiring a lock table or migration.
SCAN_ADVISORY_LOCK_KEY = 0x4D53545343414E


class ScanLockError(RuntimeError):
    """Raised when the production scan lock cannot be checked or released."""


@dataclass
class ScanExecutionLock:
    """Own one checked-out connection holding a session advisory lock."""

    connection: Connection
    lock_key: int = SCAN_ADVISORY_LOCK_KEY
    _released: bool = False

    def release(self) -> None:
        """Release the advisory lock and always return its connection."""

        if self._released:
            return
        self._released = True
        try:
            released = self.connection.scalar(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": self.lock_key},
            )
            if released is not True:
                raise ScanLockError("PostgreSQL did not release the candidate scan lock")
        except SQLAlchemyError as exc:
            # Do not return a possibly lock-holding physical connection to the
            # pool when the explicit unlock result is ambiguous.
            self.connection.invalidate()
            raise ScanLockError("Could not release the candidate scan lock") from exc
        finally:
            # PostgreSQL also releases session advisory locks when a connection
            # ends, including an ungraceful client disconnect.
            self.connection.close()

    def __enter__(self) -> "ScanExecutionLock":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


def try_acquire_scan_lock(engine: Engine) -> ScanExecutionLock | None:
    """Acquire the global scan lock immediately, returning ``None`` if held."""

    if engine.dialect.name != "postgresql":
        raise ScanLockError(
            "Candidate scan overlap protection requires PostgreSQL"
        )

    connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        acquired = connection.scalar(
            text("SELECT pg_try_advisory_lock(:lock_key)"),
            {"lock_key": SCAN_ADVISORY_LOCK_KEY},
        )
    except SQLAlchemyError as exc:
        # The server may have acquired the lock even if the response was lost.
        # Invalidating ends that physical session instead of pooling ambiguity.
        connection.invalidate()
        connection.close()
        raise ScanLockError("Could not acquire the candidate scan lock") from exc

    if acquired is not True:
        connection.close()
        return None
    logger.info("Acquired PostgreSQL candidate scan lock")
    return ScanExecutionLock(connection)
