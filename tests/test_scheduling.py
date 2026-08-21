"""Tests for Railway cron configuration and scan overlap protection."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db.locks import ScanExecutionLock, ScanLockError, try_acquire_scan_lock
from app.jobs import brand_scan
from app.jobs.brand_scan import CandidatePipelineResult
from app.services import ProviderError
from app.services.google_sheets import SheetSyncResult


class FakeConnection:
    def __init__(self, results: list[object]) -> None:
        self.results = results
        self.closed = False
        self.invalidated = False
        self.execution_options_seen: dict[str, object] | None = None

    def execution_options(self, **options: object) -> "FakeConnection":
        self.execution_options_seen = options
        return self

    def scalar(self, statement: object, parameters: object) -> object:
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def close(self) -> None:
        self.closed = True

    def invalidate(self) -> None:
        self.invalidated = True


class FakeEngine:
    def __init__(self, connection: FakeConnection, dialect: str = "postgresql") -> None:
        self.connection = connection
        self.dialect = SimpleNamespace(name=dialect)

    def connect(self) -> FakeConnection:
        return self.connection


class FakeLock:
    def __init__(self) -> None:
        self.release_calls = 0

    def release(self) -> None:
        self.release_calls += 1


class FakePersistence:
    def __init__(self, lock: FakeLock | None) -> None:
        self.lock = lock
        self.closed = False

    def try_acquire_scan_lock(self) -> FakeLock | None:
        return self.lock

    def close(self) -> None:
        self.closed = True


def production_settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql://unused:unused@localhost/unused",
        persist_scan_results=True,
        google_sheets_enabled=True,
        meta_ad_provider="apify",
        apify_api_token="test-token",
    )


def test_global_scan_timeout_default_and_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings(_env_file=None).scan_max_runtime_seconds == 2700

    monkeypatch.setenv("SCAN_MAX_RUNTIME_SECONDS", "1800")

    assert Settings(_env_file=None).scan_max_runtime_seconds == 1800


def patch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    persistence: FakePersistence,
    *,
    pipeline_error: Exception | None = None,
) -> dict[str, int]:
    calls = {"provider_builds": 0, "pipeline_runs": 0}

    monkeypatch.setattr(
        brand_scan.ScanPersistenceService,
        "from_database_url",
        lambda *args, **kwargs: persistence,
    )
    monkeypatch.setattr(brand_scan, "_build_sheets_provider", lambda settings: object())

    def build_provider(settings: Settings) -> object:
        calls["provider_builds"] += 1
        return object()

    monkeypatch.setattr(brand_scan, "_build_meta_provider", build_provider)

    class FakePipeline:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def run(self) -> CandidatePipelineResult:
            calls["pipeline_runs"] += 1
            if pipeline_error is not None:
                raise pipeline_error
            return CandidatePipelineResult(
                records=(),
                relevance_results=(),
                scan_run_id=42,
                sheet_sync=SheetSyncResult(0, 0, 0, ()),
            )

    monkeypatch.setattr(brand_scan, "CandidatePipeline", FakePipeline)
    return calls


def test_railway_cron_entry_point_exits_without_process_restarts() -> None:
    config = json.loads(Path("railway.cron.json").read_text(encoding="utf-8"))

    assert config["deploy"]["startCommand"] == (
        "python -m app.jobs.brand_scan --run-once"
    )
    assert config["deploy"]["restartPolicyType"] == "NEVER"
    assert "healthcheckPath" not in config["deploy"]

    web_config = json.loads(Path("railway.json").read_text(encoding="utf-8"))
    assert web_config["deploy"]["preDeployCommand"] == "alembic upgrade head"
    assert "preDeployCommand" not in config["deploy"]


def test_global_timeout_before_discovery_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"discovery": 0}

    async def stalled_preflight(
        settings: Settings, *, require_full_outputs: bool
    ) -> int:
        assert require_full_outputs is True
        await asyncio.sleep(10)
        calls["discovery"] += 1
        return 0

    monkeypatch.setattr(brand_scan, "_run_scan_command", stalled_preflight)
    configured = production_settings().model_copy(
        update={"scan_max_runtime_seconds": 0.01}
    )

    with pytest.raises(ProviderError, match="SCAN_MAX_RUNTIME_SECONDS"):
        asyncio.run(brand_scan._run_once(configured))

    assert calls["discovery"] == 0


def test_global_timeout_releases_scan_lock_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = FakeLock()
    persistence = FakePersistence(lock)
    calls = patch_runtime(monkeypatch, persistence)

    class SlowPipeline:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def run(self) -> CandidatePipelineResult:
            calls["pipeline_runs"] += 1
            await asyncio.sleep(10)
            raise AssertionError("unreachable")

    monkeypatch.setattr(brand_scan, "CandidatePipeline", SlowPipeline)
    configured = production_settings().model_copy(
        update={"scan_max_runtime_seconds": 0.01}
    )

    with pytest.raises(ProviderError, match="SCAN_MAX_RUNTIME_SECONDS"):
        asyncio.run(brand_scan._run_once(configured))

    assert calls == {"provider_builds": 1, "pipeline_runs": 1}
    assert lock.release_calls == 1
    assert persistence.closed is True


def test_overlap_skips_before_paid_provider_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistence = FakePersistence(lock=None)
    calls = patch_runtime(monkeypatch, persistence)

    result = asyncio.run(brand_scan._run_once(production_settings()))

    assert result == 0
    assert calls == {"provider_builds": 0, "pipeline_runs": 0}
    assert persistence.closed is True


def test_scan_lock_is_released_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
    lock = FakeLock()
    persistence = FakePersistence(lock)
    calls = patch_runtime(monkeypatch, persistence)

    result = asyncio.run(brand_scan._run_once(production_settings()))

    assert result == 0
    assert calls == {"provider_builds": 1, "pipeline_runs": 1}
    assert lock.release_calls == 1
    assert persistence.closed is True


def test_scan_lock_is_released_after_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    lock = FakeLock()
    persistence = FakePersistence(lock)
    calls = patch_runtime(
        monkeypatch,
        persistence,
        pipeline_error=ProviderError("provider failed"),
    )

    with pytest.raises(ProviderError, match="provider failed"):
        asyncio.run(brand_scan._run_once(production_settings()))

    assert calls == {"provider_builds": 1, "pipeline_runs": 1}
    assert lock.release_calls == 1
    assert persistence.closed is True


def test_postgres_lock_acquisition_is_non_blocking() -> None:
    connection = FakeConnection([False])

    lock = try_acquire_scan_lock(FakeEngine(connection))  # type: ignore[arg-type]

    assert lock is None
    assert connection.execution_options_seen == {"isolation_level": "AUTOCOMMIT"}
    assert connection.closed is True
    assert connection.invalidated is False


def test_lock_is_explicitly_released_once() -> None:
    connection = FakeConnection([True])
    lock = ScanExecutionLock(connection)  # type: ignore[arg-type]

    lock.release()
    lock.release()

    assert connection.closed is True
    assert connection.invalidated is False


def test_lock_connection_closes_if_explicit_release_fails() -> None:
    connection = FakeConnection([SQLAlchemyError("connection lost")])
    lock = ScanExecutionLock(connection)  # type: ignore[arg-type]

    with pytest.raises(ScanLockError, match="Could not release"):
        lock.release()

    assert connection.closed is True
    assert connection.invalidated is True
