"""Mocked tests for the SolidCode Apify Meta ads provider."""

import asyncio
import json
import logging

import httpx
import pytest

from app.models import Region
from app.services import ProviderError
from app.services.meta_ads import (
    APIFY_EU_COUNTRY_CODES,
    APIFY_UNAVAILABLE_EU_COUNTRY_CODES,
    ApifyMetaAdsProvider,
)


def apify_ad(
    ad_id: str = "ad-1",
    *,
    page_id: str = "page-1",
    page_name: str = "Example Supplements",
) -> dict[str, object]:
    return {
        "adArchiveID": ad_id,
        "adLibraryURL": f"https://www.facebook.com/ads/library/?id={ad_id}",
        "pageID": page_id,
        "pageName": page_name,
        "adStatus": "ACTIVE",
        "publisherPlatforms": ["FACEBOOK", "INSTAGRAM"],
        "startDate": "2026-07-01",
        "endDate": None,
        "adCreationTime": "2026-06-30T12:00:00+00:00",
        "adText": "Daily magnesium gummies",
        "adCreativeBodies": ["Daily magnesium gummies"],
        "ctaDomain": "example.test",
        "ctaUrl": "https://example.test/magnesium",
        "ctaHeadline": "Magnesium Gummies",
        "ctaDescription": "Shop our range",
        "adSnapshotUrl": f"https://www.facebook.com/ads/archive/render_ad/?id={ad_id}",
        "spend": None,
        "impressions": None,
        "reachEstimate": None,
        "estimatedAudienceSize": None,
        "regionsReached": None,
        "demographics": None,
        "source": "search",
        "sourceQuery": "magnesium",
    }


def run_provider(
    handler: httpx.MockTransport,
    *,
    regions: list[str] | None = None,
    max_results: int = 500,
    monthly_budget_gbp: float = 30,
    timeout: float = 120,
) -> list:
    async def run() -> list:
        async with httpx.AsyncClient(transport=handler) as client:
            provider = ApifyMetaAdsProvider(
                api_token="test-token",
                client=client,
                max_results_per_query=max_results,
                monthly_budget_gbp=monthly_budget_gbp,
                request_timeout_seconds=timeout,
                retry_attempts=2,
                retry_min_wait_seconds=0,
                retry_max_wait_seconds=0,
            )
            return await provider.retrieve_advertisers(
                regions=regions or ["UK"],
                categories=["vitamins", "magnesium"],
            )

    return asyncio.run(run())


def standard_handler(
    dataset_items: list[object], *, run_status: str = "SUCCEEDED"
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/users/me/limits":
            return httpx.Response(
                200, json={"data": {"current": {"monthlyUsageUsd": 0}}}
            )
        if request.url.path.endswith("/runs"):
            return httpx.Response(201, json={"data": {"id": "run-1"}})
        if request.url.path == "/v2/actor-runs/run-1":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "run-1",
                        "status": run_status,
                        "statusMessage": "Actor failed" if run_status == "FAILED" else None,
                        "defaultDatasetId": "dataset-1",
                    }
                },
            )
        if request.url.path == "/v2/datasets/dataset-1/items":
            return httpx.Response(200, json=dataset_items)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


def test_successful_actor_run_uses_documented_contract_and_normalizes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v2/users/me/limits":
            return httpx.Response(
                200, json={"data": {"current": {"monthlyUsageUsd": 1.25}}}
            )
        if request.url.path.endswith("/runs"):
            actor_input = json.loads(request.content)
            assert actor_input == {
                "searchTerms": ["vitamins", "magnesium"],
                "country": "GB",
                "adActiveStatus": "ACTIVE",
                "adType": "ALL",
                "scrapeAdDetails": True,
                "includeAboutPage": False,
                "onlyTotalCount": False,
                "maxResults": 500,
            }
            assert request.url.params["maxTotalChargeUsd"] == "0.2500"
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
            return httpx.Response(200, json=[apify_ad()])
        raise AssertionError(f"Unexpected request: {request.url}")

    records = run_provider(httpx.MockTransport(handler))

    assert len(records) == 1
    record = records[0]
    assert record.brand.name == "Example Supplements"
    assert record.brand.source_id == "page-1"
    assert record.active_ad_count == 1
    assert record.estimated_monthly_spend_usd is None
    assert record.provider_metadata["provider"] == "apify"
    assert record.ads[0].ad_id == "ad-1"
    assert record.ads[0].landing_page_url == "https://example.test/magnesium"
    assert record.ads[0].matched_countries == ["GB"]
    assert all(request.headers["authorization"] == "Bearer test-token" for request in requests)
    assert all("token" not in request.url.params for request in requests)


def test_empty_results_return_no_advertisers() -> None:
    assert run_provider(standard_handler([])) == []


def test_dataset_items_are_paginated() -> None:
    dataset_offsets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/users/me/limits":
            return httpx.Response(
                200, json={"data": {"current": {"monthlyUsageUsd": 0}}}
            )
        if request.url.path.endswith("/runs"):
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
            offset = request.url.params["offset"]
            dataset_offsets.append(offset)
            if offset == "0":
                return httpx.Response(
                    200,
                    json=[apify_ad(f"ad-{index}") for index in range(1000)],
                )
            return httpx.Response(200, json=[apify_ad("ad-1000")])
        raise AssertionError(f"Unexpected request: {request.url}")

    records = run_provider(httpx.MockTransport(handler), max_results=1001)

    assert dataset_offsets == ["0", "1000"]
    assert len(records) == 1
    assert len(records[0].ads) == 1001


def test_malformed_results_are_logged_and_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    assert run_provider(standard_handler([{"pageID": "missing-ad"}, "not-an-object"])) == []
    assert "Skipping malformed Apify ad result" in caplog.text
    assert "Skipping malformed Apify dataset item" in caplog.text


def test_timeout_aborts_actor_run() -> None:
    aborted = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal aborted
        if request.url.path == "/v2/users/me/limits":
            return httpx.Response(
                200, json={"data": {"current": {"monthlyUsageUsd": 0}}}
            )
        if request.url.path.endswith("/runs"):
            return httpx.Response(201, json={"data": {"id": "slow-run"}})
        if request.url.path == "/v2/actor-runs/slow-run":
            return httpx.Response(200, json={"data": {"status": "RUNNING"}})
        if request.url.path == "/v2/actor-runs/slow-run/abort":
            aborted = True
            return httpx.Response(200, json={"data": {"status": "ABORTING"}})
        raise AssertionError(f"Unexpected request: {request.url}")

    with pytest.raises(ProviderError, match="exceeded APIFY_REQUEST_TIMEOUT_SECONDS"):
        run_provider(httpx.MockTransport(handler), timeout=0.001)
    assert aborted


def test_actor_failure_is_reported() -> None:
    with pytest.raises(ProviderError, match="ended with FAILED: Actor failed"):
        run_provider(standard_handler([], run_status="FAILED"))


def test_ads_and_advertisers_are_deduplicated_across_regions() -> None:
    run_number = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal run_number
        if request.url.path == "/v2/users/me/limits":
            return httpx.Response(
                200, json={"data": {"current": {"monthlyUsageUsd": 0}}}
            )
        if request.url.path.endswith("/runs"):
            run_number += 1
            return httpx.Response(201, json={"data": {"id": f"run-{run_number}"}})
        if request.url.path.startswith("/v2/actor-runs/run-"):
            run_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "SUCCEEDED",
                        "defaultDatasetId": f"dataset-{run_id[-1]}",
                    }
                },
            )
        if request.url.path.startswith("/v2/datasets/dataset-"):
            return httpx.Response(200, json=[apify_ad()])
        raise AssertionError(f"Unexpected request: {request.url}")

    records = run_provider(httpx.MockTransport(handler), regions=["UK", "USA"])

    assert len(records) == 1
    assert len(records[0].ads) == 1
    assert records[0].regions == [Region.UK, Region.USA]
    assert records[0].ads[0].matched_countries == ["GB", "US"]


def test_region_mapping_uses_only_actor_schema_country_codes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = ApifyMetaAdsProvider(api_token="test-token")
    caplog.set_level(logging.WARNING)

    resolved = provider._resolve_regions(["UK", "EU", "USA", "Canada", "Mars"])

    assert resolved == [
        (Region.UK, ("GB",)),
        (Region.EUROPE, APIFY_EU_COUNTRY_CODES),
        (Region.USA, ("US",)),
        (Region.CANADA, ("CA",)),
    ]
    assert APIFY_UNAVAILABLE_EU_COUNTRY_CODES
    assert "Skipping unsupported Apify Meta-ad region Mars" in caplog.text
    assert "Actor country schema lacks these EU country codes" in caplog.text


def test_budget_guard_fails_before_starting_actor() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v2/users/me/limits":
            return httpx.Response(
                200, json={"data": {"current": {"monthlyUsageUsd": 29.90}}}
            )
        raise AssertionError("Actor must not start after budget guard rejects the plan")

    with pytest.raises(ProviderError, match="monthly budget guard aborted"):
        run_provider(httpx.MockTransport(handler))
    assert [request.url.path for request in requests] == ["/v2/users/me/limits"]


def test_transient_usage_timeout_is_retried_safely() -> None:
    usage_calls = 0
    fallback = standard_handler([])

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal usage_calls
        if request.url.path == "/v2/users/me/limits":
            usage_calls += 1
            if usage_calls == 1:
                raise httpx.ReadTimeout("temporary timeout")
        return fallback.handle_request(request)

    assert run_provider(httpx.MockTransport(handler)) == []
    assert usage_calls == 2
