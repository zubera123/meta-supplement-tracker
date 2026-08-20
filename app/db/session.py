"""Database URL normalization, engine creation, and sessions."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker


class DatabaseConfigurationError(ValueError):
    """Raised when persistence configuration is missing or unsupported."""


def normalize_database_url(database_url: str | None) -> URL:
    """Convert Railway's PostgreSQL URL to SQLAlchemy's psycopg 3 dialect."""

    if database_url is None or not database_url.strip():
        raise DatabaseConfigurationError(
            "DATABASE_URL is required when persistence is enabled"
        )

    value = database_url.strip()
    if value.startswith("postgres://"):
        value = "postgresql://" + value.removeprefix("postgres://")

    try:
        parsed = make_url(value)
    except Exception as exc:
        raise DatabaseConfigurationError("DATABASE_URL is not a valid database URL") from exc

    if parsed.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise DatabaseConfigurationError(
            "DATABASE_URL must use Railway PostgreSQL (postgresql:// or postgres://)"
        )
    return parsed.set(drivername="postgresql+psycopg")


def create_database_engine(
    database_url: str | None, *, connect_timeout_seconds: int = 10
) -> Engine:
    """Create the process-wide style SQLAlchemy engine used by persistence."""

    url = normalize_database_url(database_url)
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args={"connect_timeout": connect_timeout_seconds},
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create sessions with explicit transaction boundaries."""

    return sessionmaker(bind=engine, expire_on_commit=False)
