import asyncio
import json
import logging

import httpx
import pytest

from app.models import Region
from app.services import ProviderConfigurationError, ProviderError
from app.services.meta_ads import META_AD_FIELDS, MetaAdLibraryProvider


def run_provider(
    handler: httpx.MockTransport,
    *,
    regions: list[str] | None = None,
    categories: list[str] | None = None,
    retry_attempts: int = 3,
) -> list:
    async def run() -> list:
        async with httpx.AsyncClient(transport=handler) as client:
            provider = MetaAdLibraryProvider(
                access_token="test-token",
                client=client,
                retry_attempts=retry_attempts,
                retry_min_wait_seconds=0,
                retry_max_wait_seconds=0,
            )
            return await provider.retrieve_advertisers(
                regions=regions or ["UK"],
                categories=categories or ["vitamins"],
            )

    return asyncio.run(run())


def meta_ad(
    ad_id: str,
    *,
    start: str,
    snapshot_token: str | None = None,
) -> dict[str, object]:
    snapshot_url = f"https://www.facebook.com/ads/archive/render_ad/?id={ad_id}"
    if snapshot_token:
        snapshot_url += f"&access_token={snapshot_token}"
    return {
        "id": ad_id,
        "page_id": "page-1",
        "page_name": "Advertiser Page",
        "ad_creation_time": start,
        "ad_delivery_start_time": start,
        "ad_snapshot_url": snapshot_url,
        "ad_creative_bodies": ["Daily vitamins"],
        "publisher_platforms": ["facebook", "instagram"],
        "languages": ["en"],
        "eu_total_reach": 1234,
        "total_reach_by_location": [{"EU": 1234}],
    }


def test_discovers_supported_regions_paginates_deduplicates_and_aggregates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        countries = json.loads(request.url.params["ad_reached_countries"])
        after = request.url.params.get("after")
        if countries == ["GB"] and after is None:
            return httpx.Response(
                200,
                json={
                    "data": [
                        meta_ad(
                            "ad-1",
                            start="2026-01-01T10:00:00+0000",
                            snapshot_token="must-not-leak",
                        )
                    ],
                    "paging": {
                        "cursors": {"after": "cursor-1"},
                        "next": "https://graph.facebook.com/next",
                    },
                },
            )
        if countries == ["GB"] and after == "cursor-1":
            return httpx.Response(
                200,
                json={"data": [meta_ad("ad-2", start="2026-03-01T10:00:00+0000")]},
            )
        eu_ad = meta_ad("ad-1", start="2026-01-01T10:00:00+0000")
        eu_ad["beneficiary_payers"] = [{}]
        return httpx.Response(200, json={"data": [eu_ad]})

    caplog.set_level(logging.WARNING)
    records = run_provider(
        httpx.MockTransport(handler),
        regions=["UK", "Europe", "USA", "Canada"],
    )

    assert len(records) == 1
    record = records[0]
    assert record.brand.name == "Advertiser Page"
    assert record.brand.source_id == "page-1"
    assert record.regions == [Region.UK, Region.EUROPE]
    assert record.active_ad_count == 2
    assert [ad.ad_id for ad in record.ads] == ["ad-1", "ad-2"]
    assert record.oldest_active_ad.isoformat() == "2026-01-01T10:00:00+00:00"
    assert record.newest_active_ad.isoformat() == "2026-03-01T10:00:00+00:00"
    assert record.estimated_monthly_spend_usd is None
    assert record.ads[0].eu_total_reach == 1234
    assert record.ads[0].beneficiary_payers == [{}]
    assert "access_token" not in record.ads[0].ad_snapshot_url
    assert "must-not-leak" not in record.model_dump_json()
    assert len(requests) == 3
    assert all(request.url.path == "/v26.0/ads_archive" for request in requests)
    assert all(request.url.params["ad_type"] == "ALL" for request in requests)
    assert all(request.url.params["ad_active_status"] == "ACTIVE" for request in requests)
    assert all(request.url.params["search_type"] == "KEYWORD_EXACT_PHRASE" for request in requests)
    assert all("spend" not in request.url.params["fields"].split(",") for request in requests)
    assert set(requests[0].url.params["fields"].split(",")) == set(META_AD_FIELDS)
    requested_country_sets = [
        json.loads(request.url.params["ad_reached_countries"]) for request in requests
    ]
    assert any(len(countries) == 27 for countries in requested_country_sets)
    assert all("US" not in countries and "CA" not in countries for countries in requested_country_sets)
    assert "Skipping unsupported Meta commercial-ad region USA" in caplog.text
    assert "Skipping unsupported Meta commercial-ad region Canada" in caplog.text


def test_documented_rate_limit_error_stops_without_retrying() -> None:
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json={"error": {"code": 613, "message": "Calls exceeded rate limit"}},
        )

    with pytest.raises(ProviderError, match="rate limit 613"):
        run_provider(httpx.MockTransport(handler))
    assert call_count == 1


def test_retries_response_explicitly_marked_transient() -> None:
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                500,
                json={
                    "error": {
                        "code": 2,
                        "message": "Temporary service error",
                        "is_transient": True,
                    }
                },
            )
        return httpx.Response(200, json={"data": []})

    assert run_provider(httpx.MockTransport(handler)) == []
    assert call_count == 2


def test_non_transient_oauth_error_is_not_retried() -> None:
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            400,
            json={"error": {"code": 190, "message": "Invalid OAuth access token"}},
        )

    with pytest.raises(ProviderError, match="Meta API error 190"):
        run_provider(httpx.MockTransport(handler))
    assert call_count == 1


def test_next_page_without_cursor_fails_instead_of_returning_partial_results() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [meta_ad("ad-1", start="2026-01-01T10:00:00+0000")],
                "paging": {"next": "https://graph.facebook.com/next"},
            },
        )

    with pytest.raises(ProviderError, match="without an after cursor"):
        run_provider(httpx.MockTransport(handler))


def test_missing_access_token_is_a_configuration_error() -> None:
    with pytest.raises(ProviderConfigurationError, match="META_ACCESS_TOKEN"):
        MetaAdLibraryProvider(access_token=None)


def test_only_unsupported_regions_make_no_requests(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("The API must not be called for unsupported commercial regions")

    caplog.set_level(logging.WARNING)
    result = run_provider(
        httpx.MockTransport(handler),
        regions=["USA", "Canada"],
    )

    assert result == []
    assert "No Meta commercial-ad regions are supported" in caplog.text
