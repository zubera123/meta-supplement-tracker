"""Mocked integration tests for one complete candidate-pipeline run."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import DatabaseUnavailableError, ScanPersistenceService
from app.db.base import Base
from app.db.models import Ad, Advertiser, AdvertiserObservation, ScanRun
from app.jobs.brand_scan import CandidatePipeline, _run_once
from app.models import AdRecord, Brand, MetaAdDetails, Region, SocialStats, SpendEstimate
from app.services import ProviderError
from app.services.google_sheets import SHEET_HEADERS, GoogleSheetsProvider


class FakeMetaProvider:
    def __init__(self, records: list[AdRecord], error: Exception | None = None) -> None:
        self.records = records
        self.error = error
        self.calls = 0

    async def retrieve_advertisers(
        self, *, regions: tuple[str, ...], categories: tuple[str, ...]
    ) -> list[AdRecord]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.records


class MemorySheetsApi:
    """In-memory transport implementing only documented calls used by the provider."""

    def __init__(self) -> None:
        self.rows: list[list[object]] = [list(SHEET_HEADERS)]
        self.table: dict[str, object] | None = None
        self.metadata: dict[int, tuple[int, int]] = {}

    def get_spreadsheet(self, spreadsheet_id: str) -> dict[str, Any]:
        return {
            "spreadsheetId": spreadsheet_id,
            "sheets": [
                {
                    "properties": {
                        "sheetId": 7,
                        "title": "Candidates",
                        "gridProperties": {"rowCount": 1000},
                    },
                    "tables": [self.table] if self.table else [],
                }
            ],
        }

    def batch_update_spreadsheet(
        self, spreadsheet_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        request = body["requests"][0]
        if "addTable" in request:
            self.table = dict(request["addTable"]["table"])
            return {"replies": [{} for _ in body["requests"]]}
        if "updateTable" in request:
            assert self.table is not None
            self.table["range"] = request["updateTable"]["table"]["range"]
            return {"replies": [{}]}
        if "updateSheetProperties" in request:
            return {"replies": [{} for _ in body["requests"]]}
        if "createDeveloperMetadata" in request:
            item = request["createDeveloperMetadata"]["developerMetadata"]
            company_id = int(item["metadataValue"])
            metadata_id = company_id
            row = item["location"]["dimensionRange"]["startIndex"] + 1
            self.metadata[company_id] = (metadata_id, row)
            return {"replies": [{"createDeveloperMetadata": {"developerMetadata": {
                **item, "metadataId": metadata_id,
            }}}]}
        return {"replies": [{}]}

    def get_values(self, spreadsheet_id: str, range_name: str) -> dict[str, Any]:
        if range_name.endswith("!A1:J1"):
            return {"values": self.rows[:1]}
        return {"values": [list(row) for row in self.rows]}

    def update_values(
        self, spreadsheet_id: str, range_name: str, values: list[list[object]]
    ) -> dict[str, Any]:
        self.rows[0] = list(values[0])
        return {"updatedRows": 1}

    def batch_update_values(
        self, spreadsheet_id: str, data: list[dict[str, Any]]
    ) -> dict[str, Any]:
        for update in data:
            row_range = update["range"].split("!", 1)[1]
            row_number = int(row_range.split(":", 1)[0][1:])
            width = 6 if ":F" in row_range else 8 if ":H" in row_range else 10
            while len(self.rows) < row_number:
                self.rows.append([])
            existing = (self.rows[row_number - 1] + [""] * 10)[:10]
            existing[:width] = list(update["values"][0])
            self.rows[row_number - 1] = existing
        return {"totalUpdatedRows": len(data)}

    def search_developer_metadata(
        self, spreadsheet_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return {"matchedDeveloperMetadata": [{"developerMetadata": {
            "metadataId": metadata_id,
            "metadataKey": "meta_supplement_tracker_company_id",
            "metadataValue": str(company_id),
            "visibility": "PROJECT",
            "location": {"dimensionRange": {
                "sheetId": 7, "dimension": "ROWS",
                "startIndex": row - 1, "endIndex": row,
            }},
        }} for company_id, (metadata_id, row) in self.metadata.items()]}


class FailingSheetsProvider:
    def ensure_ready(self) -> None:
        return None

    def reconcile_managed_rows(self, row_states: object) -> object:
        return type("Result", (), {"row_states": ()})()

    def sync_candidates(self, candidates: object, row_states: object, removals: object = None) -> object:
        raise ProviderError("Google Sheets write failed")


class FailingSheetsPreflightProvider:
    def ensure_ready(self) -> None:
        raise ProviderError("Google Sheets preflight failed")


class UnavailablePersistence:
    def verify_connection(self) -> None:
        raise DatabaseUnavailableError("database unavailable")


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


def settings() -> Settings:
    return Settings(
        _env_file=None,
        scan_regions="UK",
        supplement_categories="supplements",
        persist_scan_results=True,
        google_sheets_enabled=True,
    )


def record(
    *,
    page_id: str,
    followers: int | None,
    observed_at: datetime,
    active_ads: int = 2,
    name: str | None = None,
    creative: str = "Real supplement provider copy",
    page_category: str | None = None,
) -> AdRecord:
    handle = f"brand_{page_id}"
    page_name = name or f"Brand {page_id}"
    ads = [
        MetaAdDetails(
            ad_id=f"{page_id}-ad-{index}",
            page_id=page_id,
            page_name=page_name,
            ad_delivery_start_time=observed_at - timedelta(days=index),
            creative_bodies=[creative],
            facebook_page_category=page_category,
            matched_regions=[Region.UK],
        )
        for index in range(1, active_ads + 1)
    ]
    return AdRecord(
        brand=Brand(
            name=page_name,
            source_id=page_id,
            instagram_handle=handle,
        ),
        region=Region.UK,
        regions=[Region.UK],
        active_ad_count=active_ads,
        ads=ads,
        social_stats=SocialStats(
            instagram_handle=handle,
            instagram_followers=followers,
            observed_at=observed_at,
        ),
        observed_at=observed_at,
    )


def sheets_provider(api: MemorySheetsApi) -> GoogleSheetsProvider:
    return GoogleSheetsProvider(
        spreadsheet_id="spreadsheet-id",
        sheet_tab="Candidates",
        api=api,
    )


def test_run_once_requires_persistence_and_sheets() -> None:
    without_persistence = Settings(
        _env_file=None,
        persist_scan_results=False,
        google_sheets_enabled=False,
    )
    without_sheets = Settings(
        _env_file=None,
        persist_scan_results=True,
        google_sheets_enabled=False,
    )

    with pytest.raises(ProviderError, match="PERSIST_SCAN_RESULTS=true"):
        asyncio.run(_run_once(without_persistence))
    with pytest.raises(ProviderError, match="GOOGLE_SHEETS_ENABLED=true"):
        asyncio.run(_run_once(without_sheets))


@pytest.mark.parametrize("followers", [10_000, 100_000])
def test_full_pipeline_persists_and_writes_qualifying_advertiser(
    session_factory: sessionmaker[Session], followers: int
) -> None:
    observed_at = datetime(2026, 8, 21, tzinfo=UTC)
    provider = FakeMetaProvider(
        [record(page_id="page-1", followers=followers, observed_at=observed_at)]
    )
    persistence = ScanPersistenceService(session_factory)
    sheet_api = MemorySheetsApi()

    result = asyncio.run(
        CandidatePipeline(
            settings=settings(),
            meta_ads=provider,
            persistence=persistence,
            sheets=sheets_provider(sheet_api),
        ).run()
    )

    assert provider.calls == 1
    assert result.sheet_sync is not None
    assert result.sheet_sync.appended == 1
    assert sheet_api.rows[1] == [
        "2026-08-21",
        "Brand page-1",
        "UK",
        "@brand_page-1",
        followers,
        2,
        "Unknown",
        "Unknown",
        "",
        "",
    ]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Advertiser)) == 1
        assert session.scalar(select(func.count()).select_from(Ad)) == 2
        assert (
            session.scalar(select(func.count()).select_from(AdvertiserObservation))
            == 1
        )
        observation = session.scalar(select(AdvertiserObservation))
        assert observation is not None
        assert observation.supplement_relevant is True
        assert observation.relevance_reason is not None


@pytest.mark.parametrize("followers", [9_999, 100_001, None])
def test_nonqualifying_followers_are_persisted_but_not_written(
    session_factory: sessionmaker[Session], followers: int | None
) -> None:
    item = record(
        page_id="page-filtered",
        followers=followers,
        observed_at=datetime(2026, 8, 21, tzinfo=UTC),
    )
    sheet_api = MemorySheetsApi()

    result = asyncio.run(
        CandidatePipeline(
            settings=settings(),
            meta_ads=FakeMetaProvider([item]),
            persistence=ScanPersistenceService(session_factory),
            sheets=sheets_provider(sheet_api),
        ).run()
    )

    assert result.sheet_sync is not None
    assert result.sheet_sync.appended == 0
    assert sheet_api.rows == [list(SHEET_HEADERS)]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Advertiser)) == 1
        stored = session.scalar(select(Advertiser))
        assert stored is not None
        assert stored.latest_instagram_followers == followers


@pytest.mark.parametrize(
    ("estimate", "expected_rows"),
    [
        (
            SpendEstimate(
                low_usd=8_000,
                high_usd=12_000,
                method="impressions_cpm",
                source="Impressions × CPM",
                confidence="medium",
                target_match=True,
            ),
            1,
        ),
        (
            SpendEstimate(
                low_usd=500,
                high_usd=1_000,
                method="impressions_cpm",
                source="Impressions × CPM",
                confidence="medium",
                target_match=False,
            ),
            0,
        ),
        (
            SpendEstimate(
                low_usd=40_000,
                high_usd=60_000,
                method="reach_cpm",
                source="Reach × CPM",
                confidence="low",
                target_match=False,
            ),
            0,
        ),
        (
            SpendEstimate(
                low_usd=900,
                high_usd=4_500,
                method="activity_model",
                source="Activity model - very rough",
                confidence="very_low",
                target_match=None,
            ),
            1,
        ),
        (
            SpendEstimate(
                method="unknown",
                source="Unknown",
                confidence="unknown",
                target_match=None,
            ),
            1,
        ),
    ],
)
def test_reliable_spend_result_controls_sheet_eligibility_but_not_persistence(
    session_factory: sessionmaker[Session],
    estimate: SpendEstimate,
    expected_rows: int,
) -> None:
    class FixedSpendEstimator:
        def estimate(self, item: AdRecord, history: object) -> SpendEstimate:
            return estimate

    item = record(
        page_id=f"spend-{estimate.method}-{expected_rows}",
        followers=25_000,
        observed_at=datetime(2026, 8, 21, tzinfo=UTC),
    )
    sheet_api = MemorySheetsApi()

    result = asyncio.run(
        CandidatePipeline(
            settings=settings(),
            meta_ads=FakeMetaProvider([item]),
            persistence=ScanPersistenceService(session_factory),
            sheets=sheets_provider(sheet_api),
            spend_estimator=FixedSpendEstimator(),  # type: ignore[arg-type]
        ).run()
    )

    assert result.sheet_sync is not None
    assert result.sheet_sync.appended == expected_rows
    assert len(sheet_api.rows) == expected_rows + 1
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Advertiser)) == 1
        observation = session.scalar(select(AdvertiserObservation))
        assert observation is not None
        assert observation.spend_target_match is estimate.target_match


def test_pipeline_deadline_during_discovery_records_failed_scan(
    session_factory: sessionmaker[Session],
) -> None:
    class SlowMetaProvider(FakeMetaProvider):
        async def retrieve_advertisers(
            self, *, regions: tuple[str, ...], categories: tuple[str, ...]
        ) -> list[AdRecord]:
            self.calls += 1
            await asyncio.sleep(10)
            return []

    provider = SlowMetaProvider([])
    persistence = ScanPersistenceService(session_factory)

    async def run() -> None:
        async with asyncio.timeout(0.1):
            await CandidatePipeline(
                settings=Settings(
                    _env_file=None,
                    scan_regions="UK",
                    supplement_categories="supplements",
                    persist_scan_results=True,
                    google_sheets_enabled=True,
                    scan_max_runtime_seconds=0.1,
                ),
                meta_ads=provider,
                persistence=persistence,
                sheets=sheets_provider(MemorySheetsApi()),
            ).run()

    with pytest.raises(TimeoutError):
        asyncio.run(run())

    assert provider.calls == 1
    with session_factory() as session:
        scan_run = session.scalar(select(ScanRun))
        assert scan_run is not None
        assert scan_run.status == "failed"
        assert "SCAN_MAX_RUNTIME_SECONDS" in (scan_run.error_message or "")


def test_irrelevant_advertiser_is_persisted_with_reason_but_not_written(
    session_factory: sessionmaker[Session],
) -> None:
    item = record(
        page_id="page-produce",
        name="Zespri Kiwifruit",
        creative="Fresh kiwifruit naturally high in vitamin C.",
        page_category="Food and beverage",
        followers=23_485,
        observed_at=datetime(2026, 8, 21, tzinfo=UTC),
    )
    sheet_api = MemorySheetsApi()

    result = asyncio.run(
        CandidatePipeline(
            settings=settings(),
            meta_ads=FakeMetaProvider([item]),
            persistence=ScanPersistenceService(session_factory),
            sheets=sheets_provider(sheet_api),
        ).run()
    )

    assert result.relevance_results[0].is_relevant is False
    assert result.sheet_sync is not None and result.sheet_sync.appended == 0
    assert sheet_api.rows == [list(SHEET_HEADERS)]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Advertiser)) == 1
        observation = session.scalar(select(AdvertiserObservation))
        assert observation is not None
        assert observation.supplement_relevant is False
        assert observation.relevance_reason == (
            "excluded: obvious non-supplement identity keyword(s): kiwifruit"
        )


def test_repeated_scan_updates_same_sheet_row_and_preserves_review_values(
    session_factory: sessionmaker[Session],
) -> None:
    persistence = ScanPersistenceService(session_factory)
    sheet_api = MemorySheetsApi()
    sheets = sheets_provider(sheet_api)
    first_seen = datetime(2026, 8, 1, tzinfo=UTC)

    first = asyncio.run(
        CandidatePipeline(
            settings=settings(),
            meta_ads=FakeMetaProvider(
                [record(page_id="page-repeat", followers=20_000, observed_at=first_seen)]
            ),
            persistence=persistence,
            sheets=sheets,
        ).run()
    )
    sheet_api.rows[1][6:10] = ["future spend", "future source", 450, "future reviews"]
    second = asyncio.run(
        CandidatePipeline(
            settings=settings(),
            meta_ads=FakeMetaProvider(
                [
                    record(
                        page_id="page-repeat",
                        followers=30_000,
                        observed_at=first_seen + timedelta(days=10),
                        active_ads=3,
                    )
                ]
            ),
            persistence=persistence,
            sheets=sheets,
        ).run()
    )

    assert first.sheet_sync is not None and first.sheet_sync.appended == 1
    assert second.sheet_sync is not None and second.sheet_sync.updated == 1
    assert len(sheet_api.rows) == 2
    assert sheet_api.rows[1][0] == "2026-08-01"
    assert sheet_api.rows[1][4:6] == [30_000, 3]
    assert sheet_api.rows[1][6:10] == [
        "$900–$4.5k/mo",
        "Activity model - very rough",
        450,
        "future reviews",
    ]


def test_google_sheets_failure_is_surfaced_and_scan_marked_failed(
    session_factory: sessionmaker[Session],
) -> None:
    persistence = ScanPersistenceService(session_factory)
    provider = FakeMetaProvider(
        [
            record(
                page_id="page-failure",
                followers=25_000,
                observed_at=datetime(2026, 8, 21, tzinfo=UTC),
            )
        ]
    )

    with pytest.raises(ProviderError, match="Google Sheets write failed"):
        asyncio.run(
            CandidatePipeline(
                settings=settings(),
                meta_ads=provider,
                persistence=persistence,
                sheets=FailingSheetsProvider(),  # type: ignore[arg-type]
            ).run()
        )

    with session_factory() as session:
        scan_run = session.scalar(select(ScanRun))
        assert scan_run is not None
        assert scan_run.status == "failed"
        assert "Google Sheets write failed" in (scan_run.error_message or "")


def test_database_failure_is_surfaced_before_meta_or_sheets() -> None:
    provider = FakeMetaProvider([])

    class SheetsNotCalled:
        def ensure_ready(self) -> None:
            raise AssertionError("Sheets preflight should not run after DB failure")

    with pytest.raises(DatabaseUnavailableError, match="database unavailable"):
        asyncio.run(
            CandidatePipeline(
                settings=settings(),
                meta_ads=provider,
                persistence=UnavailablePersistence(),  # type: ignore[arg-type]
                sheets=SheetsNotCalled(),  # type: ignore[arg-type]
            ).run()
        )

    assert provider.calls == 0


def test_sheets_preflight_failure_is_recorded_before_paid_meta_call(
    session_factory: sessionmaker[Session],
) -> None:
    provider = FakeMetaProvider([])

    with pytest.raises(ProviderError, match="Google Sheets preflight failed"):
        asyncio.run(
            CandidatePipeline(
                settings=settings(),
                meta_ads=provider,
                persistence=ScanPersistenceService(session_factory),
                sheets=FailingSheetsPreflightProvider(),  # type: ignore[arg-type]
            ).run()
        )

    assert provider.calls == 0
    with session_factory() as session:
        scan_run = session.scalar(select(ScanRun))
        assert scan_run is not None
        assert scan_run.status == "failed"
        assert "Google Sheets preflight failed" in (scan_run.error_message or "")


def test_paid_provider_failure_is_not_retried(
    session_factory: sessionmaker[Session],
) -> None:
    provider = FakeMetaProvider([], ProviderError("paid Actor start failed"))

    with pytest.raises(ProviderError, match="paid Actor start failed"):
        asyncio.run(
            CandidatePipeline(
                settings=settings(),
                meta_ads=provider,
                persistence=ScanPersistenceService(session_factory),
                sheets=sheets_provider(MemorySheetsApi()),
            ).run()
        )

    assert provider.calls == 1
