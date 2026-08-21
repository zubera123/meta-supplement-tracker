"""Mocked tests for Trustpilot public Business Units enrichment."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db.base import Base
from app.db.models import Advertiser, AdvertiserObservation
from app.db.service import ScanPersistenceService
from app.jobs import brand_scan
from app.jobs.brand_scan import CandidatePipeline
from app.models import (
    AdRecord,
    Brand,
    MetaAdDetails,
    Region,
    ReviewCache,
    ReviewEnrichmentResult,
    ReviewStats,
    SocialStats,
)
from app.services import ProviderError, TransientProviderError
from app.services.reviews import (
    ReviewsProvider,
    TrustpilotReviewsProvider,
    resolve_advertiser_domain,
)


NOW = datetime(2026, 8, 21, tzinfo=UTC)


def business_unit(*, reviews: int = 350, domain: str = "example.com") -> dict:
    return {
        "id": "business-unit-1",
        "displayName": "Example Supplements",
        "name": {"identifying": domain, "referring": [f"www.{domain}"]},
        "websiteUrl": f"https://{domain}",
        "numberOfReviews": {"total": reviews, "usedForTrustScoreCalculation": reviews},
        "score": {"trustScore": 4.6, "stars": 4.5},
    }


def record(*, domain: str | None = "example.com") -> AdRecord:
    return AdRecord(
        brand=Brand(name="Example Supplements", source_id="page-1"),
        region=Region.UK,
        regions=[Region.UK],
        active_ad_count=1,
        ads=[
            MetaAdDetails(
                ad_id="ad-1",
                page_id="page-1",
                page_name="Example Supplements",
                landing_page_domain=domain,
                creative_bodies=["Magnesium supplement"],
                ad_delivery_start_time=NOW - timedelta(days=30),
            )
        ],
        social_stats=SocialStats(
            instagram_followers=20_000,
            instagram_handle="example",
            observed_at=NOW,
        ),
        observed_at=NOW,
    )


def provider(
    handler,
    *,
    sleeps: list[float] | None = None,
    attempts: int = 3,
) -> TrustpilotReviewsProvider:
    client = httpx.AsyncClient(
        base_url="https://api.trustpilot.com",
        transport=httpx.MockTransport(handler),
    )

    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return TrustpilotReviewsProvider(
        api_key="test-api-key",
        client=client,
        retry_attempts=attempts,
        retry_min_wait_seconds=0.5,
        retry_max_wait_seconds=2,
        min_request_interval_seconds=0,
        sleep=sleep,
    )


def test_successful_domain_match_and_300_plus_is_desirable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/business-units/find"
        assert request.url.params["name"] == "example.com"
        assert request.headers["apikey"] == "test-api-key"
        return httpx.Response(200, json=business_unit(reviews=300))

    result = asyncio.run(provider(handler).get_by_domain("www.example.com"))

    assert result.status == "matched"
    assert result.stats is not None
    assert result.stats.review_count == 300
    assert result.stats.trust_score == 4.6
    assert result.stats.star_score == 4.5
    assert result.stats.business_unit_id == "business-unit-1"
    assert result.stats.matched_domain == "example.com"
    assert result.stats.desirable is True


def test_under_300_reviews_is_valid_but_not_desirable() -> None:
    result = asyncio.run(
        provider(lambda request: httpx.Response(200, json=business_unit(reviews=299)))
        .get_by_domain("example.com")
    )

    assert result.stats is not None
    assert result.stats.review_count == 299
    assert result.stats.desirable is False


def test_missing_domain_remains_unknown_without_guessing_brand_name() -> None:
    domain, reason = resolve_advertiser_domain(record(domain=None))

    assert domain is None
    assert "No genuine" in reason


def test_no_trustpilot_business_unit_is_unavailable() -> None:
    result = asyncio.run(
        provider(lambda request: httpx.Response(404)).get_by_domain("example.com")
    )

    assert result.status == "unavailable"
    assert result.stats is None
    assert result.attempted_domain == "example.com"


def test_malformed_public_response_is_rejected() -> None:
    malformed = business_unit()
    malformed.pop("numberOfReviews")

    with pytest.raises(ProviderError, match="numberOfReviews"):
        asyncio.run(
            provider(lambda request: httpx.Response(200, json=malformed))
            .get_by_domain("example.com")
        )


def test_transient_response_is_retried_with_bounded_wait() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500)
        return httpx.Response(200, json=business_unit())

    result = asyncio.run(provider(handler, sleeps=sleeps).get_by_domain("example.com"))

    assert result.stats is not None
    assert calls == 2
    assert sleeps == [0.5]


def test_rate_limit_retry_after_is_respected() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "1.5"})
        return httpx.Response(200, json=business_unit())

    result = asyncio.run(provider(handler, sleeps=sleeps).get_by_domain("example.com"))

    assert result.stats is not None
    assert sleeps == [1.5]


def test_cached_business_unit_id_is_refreshed_directly() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=business_unit())

    result = asyncio.run(
        provider(handler).get_by_domain(
            "example.com", business_unit_id="business-unit-1"
        )
    )

    assert result.stats is not None
    assert paths == ["/v1/business-units/business-unit-1"]


class CountingReviewsProvider(ReviewsProvider):
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.error = error

    async def get_by_domain(
        self, domain: str, *, business_unit_id: str | None = None
    ) -> ReviewEnrichmentResult:
        self.calls.append((domain, business_unit_id))
        if self.error:
            raise self.error
        stats = ReviewStats(
            source="Trustpilot",
            review_count=400,
            business_unit_id="business-unit-1",
            matched_domain=domain,
            desirable=True,
            observed_at=NOW,
        )
        return ReviewEnrichmentResult(
            status="matched",
            stats=stats,
            reason="matched",
            attempted_domain=domain,
            refreshed_at=NOW,
        )

    async def check_connection(self) -> None:
        return None


class EmptyMetaProvider:
    def __init__(self, records: list[AdRecord]) -> None:
        self.records = records

    async def retrieve_advertisers(self, **kwargs) -> list[AdRecord]:
        return self.records


def pipeline(reviews: ReviewsProvider, persistence=None) -> CandidatePipeline:
    return CandidatePipeline(
        settings=Settings(
            _env_file=None,
            scan_regions="UK",
            supplement_categories="supplements",
            trustpilot_refresh_hours=24,
        ),
        meta_ads=EmptyMetaProvider([record()]),
        persistence=persistence,
        sheets=None,
        reviews=reviews,
    )


def test_refresh_interval_reuses_fresh_cached_data() -> None:
    reviews = CountingReviewsProvider()
    stats = ReviewStats(
        source="Trustpilot",
        review_count=350,
        business_unit_id="business-unit-1",
        matched_domain="example.com",
        observed_at=datetime.now(UTC),
    )
    cache = ReviewCache(
        business_unit_id="business-unit-1",
        matched_domain="example.com",
        last_refreshed_at=datetime.now(UTC) - timedelta(hours=23),
        latest_stats=stats,
    )

    enriched = asyncio.run(pipeline(reviews)._enrich_reviews([record()], [cache]))

    assert reviews.calls == []
    assert enriched[0].review_enrichment is not None
    assert enriched[0].review_enrichment.status == "cached"


def test_stale_cache_refreshes_by_business_unit_id() -> None:
    reviews = CountingReviewsProvider()
    cache = ReviewCache(
        business_unit_id="business-unit-1",
        matched_domain="example.com",
        last_refreshed_at=datetime.now(UTC) - timedelta(hours=25),
    )

    asyncio.run(pipeline(reviews)._enrich_reviews([record()], [cache]))

    assert reviews.calls == [("example.com", "business-unit-1")]


def test_default_refresh_interval_follows_24_hour_display_requirement() -> None:
    assert Settings(_env_file=None).trustpilot_refresh_hours == 24


def test_check_reviews_does_not_build_or_call_meta_provider(monkeypatch, capsys) -> None:
    reviews = CountingReviewsProvider()
    monkeypatch.setattr(
        brand_scan, "_build_reviews_provider", lambda settings: reviews
    )
    monkeypatch.setattr(
        brand_scan,
        "_build_meta_provider",
        lambda settings: pytest.fail("Meta provider must not be built"),
    )

    assert asyncio.run(
        brand_scan._check_reviews(
            Settings(
                _env_file=None,
                reviews_provider="trustpilot",
                trustpilot_api_key="test-api-key",
            )
        )
    ) == 0
    assert '"meta_provider_called": false' in capsys.readouterr().out


def test_trustpilot_failure_does_not_break_candidate_pipeline() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    persistence = ScanPersistenceService(factory)
    reviews = CountingReviewsProvider(TransientProviderError("unavailable"))

    result = asyncio.run(pipeline(reviews, persistence).run())

    assert len(result.records) == 1
    assert result.records[0].review_enrichment is not None
    assert result.records[0].review_enrichment.status == "error"
    with factory() as session:
        advertiser = session.scalar(select(Advertiser))
        observation = session.scalar(select(AdvertiserObservation))
        assert advertiser is not None
        assert observation is not None
        assert observation.review_status == "error"
        assert observation.review_count is None
    engine.dispose()
