"""Mocked tests for the Apify Trustpilot business-metadata provider."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.jobs import brand_scan
from app.models import ReviewCache, ReviewStats
from app.services import ProviderError
from app.services.reviews import (
    ApifyTrustpilotReviewsProvider,
    resolve_advertiser_domain,
)
from tests.test_trustpilot_reviews import CountingReviewsProvider, pipeline, record


def business_result(*, reviews: int = 350, domain: str = "example.com") -> dict:
    return {
        "type": "business",
        "businessId": "business-unit-1",
        "name": "Example Supplements",
        "domain": domain,
        "trustScore": 4.6,
        "stars": 4.5,
        "numberOfReviews": reviews,
        "website": f"https://{domain}",
        "profileUrl": f"https://www.trustpilot.com/review/{domain}",
    }


def limits(usage: float = 1.0) -> httpx.Response:
    return httpx.Response(
        200, json={"data": {"current": {"monthlyUsageUsd": usage}}}
    )


def provider(
    handler,
    *,
    timeout: float = 5,
    attempts: int = 3,
    monthly_budget_gbp: float = 30,
    charge_ceiling: float = 0.01,
    lookup_limiter=None,
    daily_limit: int = 10,
) -> ApifyTrustpilotReviewsProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def no_sleep(seconds: float) -> None:
        del seconds

    return ApifyTrustpilotReviewsProvider(
        api_token="test-apify-token",
        client=client,
        request_timeout_seconds=timeout,
        retry_attempts=attempts,
        retry_min_wait_seconds=0,
        retry_max_wait_seconds=0,
        monthly_budget_gbp=monthly_budget_gbp,
        max_total_charge_usd_per_run=charge_ceiling,
        max_unique_lookups_per_day=daily_limit,
        lookup_limiter=lookup_limiter,
        sleep=no_sleep,
    )


def successful_handler(
    *, reviews: int = 350, domain: str = "example.com"
):
    starts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/users/me/limits":
            return limits()
        if request.method == "POST" and request.url.path.endswith("/runs"):
            starts.append(request)
            return httpx.Response(201, json={"data": {"id": "run-1"}})
        if request.url.path == "/v2/actor-runs/run-1":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "SUCCEEDED",
                        "defaultDatasetId": "dataset-1",
                    }
                },
            )
        if request.url.path == "/v2/datasets/dataset-1/items":
            return httpx.Response(
                200, json=[business_result(reviews=reviews, domain=domain)]
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    return handler, starts


def test_successful_lookup_uses_exact_direct_business_input() -> None:
    handler, starts = successful_handler(reviews=300)

    result = asyncio.run(provider(handler).get_by_domain("www.example.com"))

    assert result.status == "matched"
    assert result.stats is not None
    assert result.stats.review_count == 300
    assert result.stats.trust_score == 4.6
    assert result.stats.star_score == 4.5
    assert str(result.stats.profile_url) == (
        "https://www.trustpilot.com/review/example.com"
    )
    assert result.stats.source == "Trustpilot via Apify"
    assert result.stats.desirable is True
    assert len(starts) == 1
    assert json.loads(starts[0].content) == {
        "mode": "reviews",
        "businessUrls": ["example.com"],
        "maxResults": 1,
    }
    assert starts[0].url.params["maxItems"] == "2"
    assert starts[0].url.params["maxTotalChargeUsd"] == "0.01"
    assert starts[0].url.params["restartOnError"] == "false"


def test_business_record_is_selected_while_review_record_is_ignored() -> None:
    handler, _ = successful_handler(reviews=3419, domain="uk.protein.com")

    def review_first(request: httpx.Request) -> httpx.Response:
        response = handler(request)
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json=[
                {
                    "type": "review",
                    "businessDomain": "uk.protein.com",
                    "rating": 1,
                    "text": "must not be normalized or retained",
                },
                business_result(reviews=3419, domain="uk.protein.com"),
            ])
        return response

    result = asyncio.run(
        provider(review_first).get_by_domain("uk.protein.com")
    )

    assert result.status == "matched"
    assert result.stats is not None
    assert result.stats.review_count == 3419
    assert result.stats.trust_score == 4.6
    assert result.stats.star_score == 4.5
    assert result.stats.matched_domain == "uk.protein.com"


def test_under_300_reviews_is_valid_but_not_desirable() -> None:
    handler, _ = successful_handler(reviews=299)

    result = asyncio.run(provider(handler).get_by_domain("example.com"))

    assert result.stats is not None
    assert result.stats.review_count == 299
    assert result.stats.desirable is False


def test_empty_actor_result_remains_unknown() -> None:
    handler, _ = successful_handler()

    def empty(request: httpx.Request) -> httpx.Response:
        response = handler(request)
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json=[])
        return response

    result = asyncio.run(provider(empty).get_by_domain("example.com"))

    assert result.status == "unavailable"
    assert result.stats is None


def test_missing_domain_does_not_start_actor() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    result = asyncio.run(provider(handler).get_by_domain("not-a-domain"))

    assert result.status == "unavailable"
    assert calls == 0


def test_apify_domain_resolution_does_not_fall_back_to_brand_website() -> None:
    candidate = record(domain=None).model_copy(
        update={
            "brand": record(domain=None).brand.model_copy(
                update={"website": "https://guessed-from-profile.example"}
            )
        }
    )

    domain, _ = resolve_advertiser_domain(
        candidate, include_brand_website=False
    )

    assert domain is None


def test_actor_failure_is_surfaced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/users/me/limits":
            return limits()
        if request.method == "POST" and request.url.path.endswith("/runs"):
            return httpx.Response(201, json={"data": {"id": "run-1"}})
        return httpx.Response(200, json={"data": {"status": "FAILED"}})

    with pytest.raises(ProviderError, match="status FAILED"):
        asyncio.run(provider(handler).get_by_domain("example.com"))


def test_timeout_aborts_actor_without_start_retry() -> None:
    starts = 0
    aborts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal starts, aborts
        if request.url.path == "/v2/users/me/limits":
            return limits()
        if request.method == "POST" and request.url.path.endswith("/runs"):
            starts += 1
            return httpx.Response(201, json={"data": {"id": "run-1"}})
        if request.url.path.endswith("/abort"):
            aborts += 1
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(200, json={"data": {"status": "RUNNING"}})

    with pytest.raises(ProviderError, match="exceeded"):
        asyncio.run(
            provider(handler, timeout=0.001).get_by_domain("example.com")
        )
    assert starts == 1
    assert aborts == 1


def test_malformed_result_is_rejected() -> None:
    handler, _ = successful_handler()

    def malformed(request: httpx.Request) -> httpx.Response:
        response = handler(request)
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json=[{"type": "business"}])
        return response

    with pytest.raises(ProviderError, match="businessId"):
        asyncio.run(provider(malformed).get_by_domain("example.com"))


def test_paid_actor_start_is_never_retried() -> None:
    starts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal starts
        if request.url.path == "/v2/users/me/limits":
            return limits()
        if request.method == "POST":
            starts += 1
            raise httpx.ReadTimeout("ambiguous start", request=request)
        raise AssertionError("No request should follow an ambiguous paid start")

    with pytest.raises(ProviderError, match="without a safe retry"):
        asyncio.run(provider(handler).get_by_domain("example.com"))
    assert starts == 1


def test_exact_domain_mismatch_remains_unknown() -> None:
    handler, _ = successful_handler(domain="other.example")

    result = asyncio.run(provider(handler).get_by_domain("example.com"))

    assert result.status == "unavailable"
    assert result.stats is None


def test_cached_result_reuse_and_24_hour_refresh() -> None:
    reviews = CountingReviewsProvider()
    stats = ReviewStats(
        source="Trustpilot via Apify",
        review_count=350,
        business_unit_id="business-unit-1",
        matched_domain="example.com",
        observed_at=datetime.now(UTC),
    )
    fresh = ReviewCache(
        business_unit_id="business-unit-1",
        matched_domain="example.com",
        last_refreshed_at=datetime.now(UTC) - timedelta(hours=23),
        latest_stats=stats,
    )
    stale = fresh.model_copy(
        update={"last_refreshed_at": datetime.now(UTC) - timedelta(hours=25)}
    )

    asyncio.run(pipeline(reviews)._enrich_reviews([record()], [fresh]))
    assert reviews.calls == []
    asyncio.run(pipeline(reviews)._enrich_reviews([record()], [stale]))
    assert reviews.calls == [("example.com", "business-unit-1")]


class DailyLimiter:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.domains: set[str] = set()

    def reserve_trustpilot_paid_lookup(
        self, domain: str, daily_limit: int
    ) -> tuple[bool, str]:
        assert daily_limit == self.limit
        if domain in self.domains:
            return False, "domain already looked up today"
        if len(self.domains) >= self.limit:
            return False, "UTC daily unique paid lookup limit is exhausted"
        self.domains.add(domain)
        return True, "reserved"


def test_ten_per_day_limit_stops_an_eleventh_paid_start() -> None:
    limiter = DailyLimiter(10)
    starts = 0
    current_domain = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal starts, current_domain
        if request.url.path == "/v2/users/me/limits":
            return limits()
        if request.method == "POST" and request.url.path.endswith("/runs"):
            starts += 1
            current_domain = json.loads(request.content)["businessUrls"][0]
            return httpx.Response(201, json={"data": {"id": "run-1"}})
        if request.url.path == "/v2/actor-runs/run-1":
            return httpx.Response(200, json={"data": {
                "status": "SUCCEEDED", "defaultDatasetId": "dataset-1"
            }})
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json=[business_result(domain=current_domain)])
        raise AssertionError(f"Unexpected request: {request.url.path}")

    review_provider = provider(handler, lookup_limiter=limiter, daily_limit=10)
    for index in range(10):
        result = asyncio.run(
            review_provider.get_by_domain(f"brand-{index}.example.com")
        )
        assert result.status == "matched"
    deferred = asyncio.run(
        review_provider.get_by_domain("brand-10.example.com")
    )

    assert deferred.status == "deferred"
    assert deferred.stats is None
    assert "limit" in deferred.reason
    assert starts == 10


def test_fresh_cache_remains_usable_after_daily_limit() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("Fresh cache must bypass the Actor and daily limiter")

    limiter = DailyLimiter(10)
    limiter.domains = {f"used-{index}.example" for index in range(10)}
    review_provider = provider(handler, lookup_limiter=limiter, daily_limit=10)
    stats = ReviewStats(
        source="Trustpilot via Apify",
        review_count=3419,
        trust_score=4.6,
        star_score=4.5,
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

    enriched = asyncio.run(
        pipeline(review_provider)._enrich_reviews([record()], [cache])
    )

    assert enriched[0].review_enrichment is not None
    assert enriched[0].review_enrichment.status == "cached"
    assert enriched[0].review_enrichment.stats is not None
    assert enriched[0].review_enrichment.stats.review_count == 3419
    assert calls == 0


def test_deferred_lookup_does_not_fail_candidate_enrichment() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path == "/v2/users/me/limits":
            return limits()
        raise AssertionError("Daily limit must prevent the paid Actor start")

    limiter = DailyLimiter(10)
    limiter.domains = {f"used-{index}.example" for index in range(10)}
    review_provider = provider(handler, lookup_limiter=limiter, daily_limit=10)
    enriched = asyncio.run(
        pipeline(review_provider)._enrich_reviews([record()], [ReviewCache()])
    )

    assert enriched[0].review_enrichment is not None
    assert enriched[0].review_enrichment.status == "deferred"
    assert enriched[0].review_enrichment.stats is None
    assert "limit" in enriched[0].review_enrichment.reason
    assert calls == 1  # free monthly-budget preflight only


def test_overall_monthly_budget_guard_prevents_paid_start() -> None:
    starts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal starts
        if request.url.path == "/v2/users/me/limits":
            return limits(29.995)
        if request.method == "POST":
            starts += 1
        return httpx.Response(500)

    with pytest.raises(ProviderError, match="monthly budget guard"):
        asyncio.run(provider(handler).get_by_domain("example.com"))
    assert starts == 0


def test_review_charge_ceiling_conflict_is_rejected_by_settings() -> None:
    with pytest.raises(ValidationError, match="APIFY_TRUSTPILOT"):
        Settings(
            _env_file=None,
            apify_monthly_budget_gbp=0.005,
            apify_max_total_charge_usd_per_run=0.001,
            apify_trustpilot_max_total_charge_usd_per_run=0.01,
        )


def test_daily_lookup_limit_defaults_and_environment_override(monkeypatch) -> None:
    monkeypatch.delenv("TRUSTPILOT_MAX_UNIQUE_LOOKUPS_PER_DAY", raising=False)
    assert Settings(
        _env_file=None
    ).trustpilot_max_unique_lookups_per_day == 10

    monkeypatch.setenv("TRUSTPILOT_MAX_UNIQUE_LOOKUPS_PER_DAY", "7")
    assert Settings(
        _env_file=None
    ).trustpilot_max_unique_lookups_per_day == 7


def test_nonpositive_daily_lookup_limit_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, trustpilot_max_unique_lookups_per_day=0)


def test_provider_selection_uses_apify_as_the_practical_default() -> None:
    selected = brand_scan._build_reviews_provider(
        Settings(_env_file=None, apify_api_token="test-apify-token")
    )

    assert isinstance(selected, ApifyTrustpilotReviewsProvider)
    asyncio.run(selected.close())


def test_check_reviews_verifies_actor_without_starting_it(
    capsys, monkeypatch
) -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path == "/v2/users/me/limits":
            return limits()
        if request.url.path.endswith("automation-lab~trustpilot-scraper"):
            return httpx.Response(200, json={"data": {"id": "actor-1"}})
        raise AssertionError(f"Unexpected request: {request.url.path}")

    review_provider = provider(handler)
    monkeypatch.setattr(
        brand_scan, "_build_reviews_provider", lambda settings: review_provider
    )
    assert asyncio.run(
        brand_scan._check_reviews(
            Settings(
                _env_file=None,
                reviews_provider="apify_trustpilot",
                apify_api_token="test-apify-token",
            )
        )
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["actor_available"] is True
    assert output["actor_started"] is False
    assert output["meta_provider_called"] is False
    assert methods == ["GET", "GET"]
