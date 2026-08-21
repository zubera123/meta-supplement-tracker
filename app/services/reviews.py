"""Trustpilot review enrichment using the documented public Business Units API."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote, urlsplit

import httpx

from app.models import AdRecord, Brand, ReviewEnrichmentResult, ReviewStats
from app.services import (
    ProviderConfigurationError,
    ProviderError,
    TransientProviderError,
)


logger = logging.getLogger(__name__)
TRUSTPILOT_BASE_URL = "https://api.trustpilot.com"
APIFY_API_BASE_URL = "https://api.apify.com/v2"
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
_APIFY_TERMINAL_STATUSES = frozenset(
    {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}
)


class ReviewsProvider(ABC):
    """Provider contract for domain-based public review enrichment."""

    @abstractmethod
    async def get_by_domain(
        self, domain: str, *, business_unit_id: str | None = None
    ) -> ReviewEnrichmentResult:
        """Return current public review data for a verified base domain."""

    @abstractmethod
    async def check_connection(self) -> None:
        """Verify credentials/API access without invoking Meta discovery."""

    async def close(self) -> None:
        """Release provider-owned resources, if any."""

    async def get_review_stats(self, brand: Brand) -> ReviewStats | None:
        """Compatibility helper for callers that already hold a genuine website."""

        if brand.website is None:
            return None
        domain = normalize_domain(str(brand.website))
        if domain is None:
            return None
        return (await self.get_by_domain(domain)).stats


class UnconfiguredReviewsProvider(ReviewsProvider):
    async def get_by_domain(
        self, domain: str, *, business_unit_id: str | None = None
    ) -> ReviewEnrichmentResult:
        raise ProviderConfigurationError("No reviews provider has been configured")

    async def check_connection(self) -> None:
        raise ProviderConfigurationError("No reviews provider has been configured")


class ApifyTrustpilotReviewsProvider(ReviewsProvider):
    """Resolve one exact advertiser domain through an Apify business search."""

    def __init__(
        self,
        *,
        api_token: str | None,
        actor_id: str = "automation-lab/trustpilot-scraper",
        max_total_charge_usd_per_run: float = 0.01,
        minimum_desirable_reviews: int = 300,
        monthly_budget_gbp: float = 30.0,
        budget_gbp_per_usd: float = 1.0,
        request_timeout_seconds: float = 120.0,
        retry_attempts: int = 3,
        retry_min_wait_seconds: float = 1.0,
        retry_max_wait_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if api_token is None or not api_token.strip():
            raise ProviderConfigurationError(
                "APIFY_API_TOKEN is required when REVIEWS_PROVIDER=apify_trustpilot"
            )
        if not actor_id.strip():
            raise ProviderConfigurationError(
                "APIFY_TRUSTPILOT_ACTOR_ID must not be empty"
            )
        if max_total_charge_usd_per_run <= 0:
            raise ProviderConfigurationError(
                "APIFY_TRUSTPILOT_MAX_TOTAL_CHARGE_USD_PER_RUN must be greater than 0"
            )
        if monthly_budget_gbp <= 0 or budget_gbp_per_usd <= 0:
            raise ProviderConfigurationError(
                "Apify budget and GBP-per-USD conversion must be positive"
            )
        ceiling = Decimal(str(max_total_charge_usd_per_run))
        monthly_budget = Decimal(str(monthly_budget_gbp))
        gbp_per_usd = Decimal(str(budget_gbp_per_usd))
        if ceiling * gbp_per_usd > monthly_budget:
            raise ProviderConfigurationError(
                "APIFY_TRUSTPILOT_MAX_TOTAL_CHARGE_USD_PER_RUN exceeds the "
                "configured monthly Apify budget after conversion"
            )

        self._api_token = api_token.strip()
        self._headers = {"Authorization": f"Bearer {self._api_token}"}
        self._actor_id = actor_id.strip()
        self._actor_ref = quote(self._actor_id.replace("/", "~"), safe="~")
        self._max_total_charge_usd_per_run = ceiling
        self._minimum_desirable_reviews = minimum_desirable_reviews
        self._monthly_budget_gbp = monthly_budget
        self._gbp_per_usd = gbp_per_usd
        self._request_timeout_seconds = request_timeout_seconds
        self._retry_attempts = retry_attempts
        self._retry_min_wait_seconds = retry_min_wait_seconds
        self._retry_max_wait_seconds = retry_max_wait_seconds
        self._reserved_maximum_usd = Decimal("0")
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=request_timeout_seconds,
            follow_redirects=False,
            headers=self._headers,
        )

    async def get_by_domain(
        self, domain: str, *, business_unit_id: str | None = None
    ) -> ReviewEnrichmentResult:
        """Search only the supplied real domain and require an exact domain result."""

        del business_unit_id  # Fresh caches avoid calls; this Actor searches by domain.
        normalized = normalize_domain(domain)
        if normalized is None:
            return unavailable_review("The supplied advertiser domain is invalid")

        await self._guard_monthly_budget()
        items = await self._run_actor_once(normalized)
        refreshed_at = datetime.now(UTC)
        if not items:
            return unavailable_review(
                "Apify Trustpilot search returned no business for the supplied domain",
                domain=normalized,
                refreshed_at=refreshed_at,
            )
        item = items[0]
        if not isinstance(item, dict):
            raise ProviderError("Apify Trustpilot dataset item is not an object")
        stats = _parse_apify_business(
            item,
            expected_domain=normalized,
            minimum_desirable_reviews=self._minimum_desirable_reviews,
            observed_at=refreshed_at,
        )
        if stats is None:
            return unavailable_review(
                "Apify Trustpilot business result did not exactly match the supplied domain",
                domain=normalized,
                refreshed_at=refreshed_at,
            )
        return ReviewEnrichmentResult(
            status="matched",
            stats=stats,
            reason="Matched Trustpilot business through Apify by exact advertiser domain",
            attempted_domain=normalized,
            refreshed_at=refreshed_at,
        )

    async def check_connection(self) -> None:
        """Verify the token, account limits, and Actor metadata without starting it."""

        await self._get_monthly_usage_usd()
        payload = await self._get_json_object_with_retry(
            f"{APIFY_API_BASE_URL}/actors/{self._actor_ref}"
        )
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise ProviderError("Apify Actor metadata response is malformed")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _guard_monthly_budget(self) -> None:
        current_usage_usd = await self._get_monthly_usage_usd()
        projected_usd = (
            current_usage_usd
            + self._reserved_maximum_usd
            + self._max_total_charge_usd_per_run
        )
        projected_gbp = projected_usd * self._gbp_per_usd
        if projected_gbp > self._monthly_budget_gbp:
            raise ProviderError(
                "Apify monthly budget guard aborted Trustpilot enrichment: current "
                "usage plus reserved review ceilings would exceed "
                f"APIFY_MONTHLY_BUDGET_GBP={self._monthly_budget_gbp:.2f}"
            )
        # Reserve before the paid start. A lost start response is ambiguous and must
        # remain charged against this process's conservative budget projection.
        self._reserved_maximum_usd += self._max_total_charge_usd_per_run

    async def _get_monthly_usage_usd(self) -> Decimal:
        payload = await self._get_json_object_with_retry(
            f"{APIFY_API_BASE_URL}/users/me/limits"
        )
        data = payload.get("data")
        current = data.get("current") if isinstance(data, dict) else None
        value = current.get("monthlyUsageUsd") if isinstance(current, dict) else None
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ProviderError(
                "Apify limits response did not contain current.monthlyUsageUsd; "
                "review budget guard fails closed"
            )
        return Decimal(str(value))

    async def _run_actor_once(self, domain: str) -> list[object]:
        endpoint = f"{APIFY_API_BASE_URL}/acts/{self._actor_ref}/runs"
        actor_input = {
            "mode": "search",
            "searchQueries": [domain],
            "maxResults": 1,
        }
        params = {
            "timeout": str(max(1, int(self._request_timeout_seconds))),
            "maxItems": "1",
            "maxTotalChargeUsd": format(
                self._max_total_charge_usd_per_run, "f"
            ),
            "restartOnError": "false",
        }
        try:
            response = await self._client.post(
                endpoint,
                params=params,
                json=actor_input,
                headers=self._headers,
            )
        except httpx.RequestError as exc:
            # A paid start is deliberately never retried because a lost response
            # could otherwise create a second billable Actor run.
            raise ProviderError(
                "Apify Trustpilot Actor start failed without a safe retry"
            ) from exc
        payload = _apify_response_object(response, "Trustpilot Actor start")
        data = payload.get("data")
        run_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(run_id, str) or not run_id:
            raise ProviderError(
                "Apify Trustpilot Actor start response did not contain data.id"
            )

        run = await self._wait_for_run(run_id)
        status = run.get("status")
        if status != "SUCCEEDED":
            raise ProviderError(f"Apify Trustpilot Actor ended with status {status}")
        dataset_id = run.get("defaultDatasetId")
        if not isinstance(dataset_id, str) or not dataset_id:
            raise ProviderError(
                "Successful Apify Trustpilot Actor run has no defaultDatasetId"
            )
        return await self._get_json_list_with_retry(
            f"{APIFY_API_BASE_URL}/datasets/{quote(dataset_id, safe='')}/items",
            params={
                "clean": "true",
                "format": "json",
                "limit": "1",
                "fields": (
                    "type,businessId,name,domain,trustScore,stars,"
                    "numberOfReviews,website,profileUrl"
                ),
            },
        )

    async def _wait_for_run(self, run_id: str) -> dict[str, object]:
        deadline = time.monotonic() + self._request_timeout_seconds
        endpoint = f"{APIFY_API_BASE_URL}/actor-runs/{quote(run_id, safe='')}"
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await self._abort_run(run_id)
                raise ProviderError(
                    "Apify Trustpilot Actor exceeded APIFY_REQUEST_TIMEOUT_SECONDS"
                )
            payload = await self._get_json_object_with_retry(
                endpoint,
                params={"waitForFinish": str(max(1, min(30, int(remaining))))},
            )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ProviderError("Apify Trustpilot run response has no data object")
            status = data.get("status")
            if status in _APIFY_TERMINAL_STATUSES:
                return data
            if not isinstance(status, str):
                raise ProviderError("Apify Trustpilot run response has no status")

    async def _abort_run(self, run_id: str) -> None:
        try:
            await self._client.post(
                f"{APIFY_API_BASE_URL}/actor-runs/{quote(run_id, safe='')}/abort",
                headers=self._headers,
            )
        except httpx.RequestError:
            logger.exception("Could not abort timed-out Apify Trustpilot Actor run")

    async def _get_json_object_with_retry(
        self, endpoint: str, *, params: dict[str, str] | None = None
    ) -> dict[str, object]:
        result = await self._get_with_retry(endpoint, params=params)
        if not isinstance(result, dict):
            raise ProviderError("Apify API returned a non-object JSON response")
        return result

    async def _get_json_list_with_retry(
        self, endpoint: str, *, params: dict[str, str] | None = None
    ) -> list[object]:
        result = await self._get_with_retry(endpoint, params=params)
        if not isinstance(result, list):
            raise ProviderError("Apify dataset API returned a non-list JSON response")
        return result

    async def _get_with_retry(
        self, endpoint: str, *, params: dict[str, str] | None = None
    ) -> object:
        for attempt in range(1, self._retry_attempts + 1):
            try:
                response = await self._client.get(
                    endpoint, params=params, headers=self._headers
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self._retry_attempts:
                    raise TransientProviderError(
                        "Apify API request failed after transient network errors"
                    ) from exc
                await self._sleep(self._retry_wait(attempt))
                continue
            if response.status_code in _TRANSIENT_STATUSES:
                if attempt >= self._retry_attempts:
                    raise TransientProviderError(
                        f"Apify API remained unavailable (HTTP {response.status_code})"
                    )
                await self._sleep(self._retry_wait(attempt))
                continue
            return _apify_response_json(response, "Apify API request")
        raise RuntimeError("Apify retry loop ended without a response")

    def _retry_wait(self, attempt: int) -> float:
        return min(
            self._retry_min_wait_seconds * (2 ** (attempt - 1)),
            self._retry_max_wait_seconds,
        )


class TrustpilotReviewsProvider(ReviewsProvider):
    """Read public Business Unit totals with persistent-ID refresh support."""

    def __init__(
        self,
        *,
        api_key: str | None,
        minimum_desirable_reviews: int = 300,
        request_timeout_seconds: float = 30,
        retry_attempts: int = 3,
        retry_min_wait_seconds: float = 1,
        retry_max_wait_seconds: float = 10,
        min_request_interval_seconds: float = 0.4,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if api_key is None or not api_key.strip():
            raise ProviderConfigurationError(
                "TRUSTPILOT_API_KEY is required when Trustpilot reviews are enabled"
            )
        self._api_key = api_key.strip()
        self._minimum_desirable_reviews = minimum_desirable_reviews
        self._retry_attempts = retry_attempts
        self._retry_min_wait_seconds = retry_min_wait_seconds
        self._retry_max_wait_seconds = retry_max_wait_seconds
        self._sleep = sleep
        self._min_request_interval_seconds = min_request_interval_seconds
        self._last_request_at: float | None = None
        self._rate_lock = asyncio.Lock()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=TRUSTPILOT_BASE_URL,
            timeout=request_timeout_seconds,
            headers={"apikey": self._api_key, "Accept": "application/json"},
        )

    async def get_by_domain(
        self, domain: str, *, business_unit_id: str | None = None
    ) -> ReviewEnrichmentResult:
        normalized = normalize_domain(domain)
        if normalized is None:
            return unavailable_review("The supplied advertiser domain is invalid")

        payload: dict[str, object] | None = None
        if business_unit_id:
            payload = await self._request(
                f"/v1/business-units/{business_unit_id}", allow_not_found=True
            )
        if payload is None:
            payload = await self._request(
                "/v1/business-units/find",
                params={"name": normalized},
                allow_not_found=True,
            )
        refreshed_at = datetime.now(UTC)
        if payload is None:
            return unavailable_review(
                "Trustpilot has no public business unit for the supplied domain",
                domain=normalized,
                refreshed_at=refreshed_at,
            )
        stats = _parse_business_unit(
            payload,
            expected_domain=normalized,
            minimum_desirable_reviews=self._minimum_desirable_reviews,
            observed_at=refreshed_at,
        )
        if stats is None:
            return unavailable_review(
                "Trustpilot returned a business unit whose identifying domains did not match",
                domain=normalized,
                refreshed_at=refreshed_at,
            )
        return ReviewEnrichmentResult(
            status="matched",
            stats=stats,
            reason="Matched Trustpilot business unit by advertiser domain",
            attempted_domain=normalized,
            refreshed_at=refreshed_at,
        )

    async def check_connection(self) -> None:
        result = await self.get_by_domain("trustpilot.com")
        if result.stats is None:
            raise ProviderError("Trustpilot API connectivity check returned no business unit")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        allow_not_found: bool = False,
    ) -> dict[str, object] | None:
        for attempt in range(1, self._retry_attempts + 1):
            try:
                await self._pace_requests()
                response = await self._client.get(
                    path,
                    params=params,
                    headers={"apikey": self._api_key, "Accept": "application/json"},
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self._retry_attempts:
                    raise TransientProviderError(
                        "Trustpilot API request failed after transient network errors"
                    ) from exc
                await self._sleep(self._retry_wait(attempt, None))
                continue

            if response.status_code == 404 and allow_not_found:
                return None
            if response.status_code in _TRANSIENT_STATUSES:
                if attempt >= self._retry_attempts:
                    raise TransientProviderError(
                        f"Trustpilot API remained unavailable (HTTP {response.status_code})"
                    )
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                wait = self._retry_wait(attempt, retry_after)
                if retry_after is not None and retry_after > self._retry_max_wait_seconds:
                    raise TransientProviderError(
                        "Trustpilot rate limit requires a wait longer than this scan allows"
                    )
                logger.warning(
                    "Transient Trustpilot API response; retrying",
                    extra={"status": response.status_code, "attempt": attempt},
                )
                await self._sleep(wait)
                continue
            if response.status_code in {401, 403}:
                raise ProviderConfigurationError(
                    "Trustpilot rejected the API key or API-module access"
                )
            if response.status_code >= 400:
                raise ProviderError(
                    f"Trustpilot API request failed with HTTP {response.status_code}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise ProviderError("Trustpilot API returned malformed JSON") from exc
            if not isinstance(payload, dict):
                raise ProviderError("Trustpilot API returned a non-object response")
            return payload
        raise RuntimeError("Trustpilot retry loop ended without a response")

    async def _pace_requests(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            if self._last_request_at is not None:
                wait = self._min_request_interval_seconds - (
                    now - self._last_request_at
                )
                if wait > 0:
                    await self._sleep(wait)
            self._last_request_at = time.monotonic()

    def _retry_wait(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return retry_after
        return min(
            self._retry_min_wait_seconds * (2 ** (attempt - 1)),
            self._retry_max_wait_seconds,
        )


def resolve_advertiser_domain(
    record: AdRecord, *, include_brand_website: bool = True
) -> tuple[str | None, str]:
    """Resolve one consistent real ad destination without brand-name guessing."""

    candidates: set[str] = set()
    if include_brand_website and record.brand.website is not None:
        domain = normalize_domain(str(record.brand.website))
        if domain:
            candidates.add(domain)
    documented_domains = {
        domain
        for ad in record.ads
        if ad.landing_page_domain
        and (domain := normalize_domain(ad.landing_page_domain)) is not None
    }
    candidates.update(documented_domains)
    if not documented_domains:
        candidates.update(
            domain
            for ad in record.ads
            if ad.landing_page_url
            and (domain := normalize_domain(ad.landing_page_url)) is not None
        )
    if not candidates:
        return None, "No genuine landing-page domain or URL was returned"
    if len(candidates) > 1:
        return None, "Advertiser ads returned conflicting landing-page domains"
    return next(iter(candidates)), "Resolved from a genuine advertiser destination"


def normalize_domain(value: str) -> str | None:
    """Return an API-safe hostname without attempting registrable-domain guessing."""

    raw = value.strip()
    if not raw:
        return None
    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    hostname = parsed.hostname
    if hostname is None:
        return None
    hostname = hostname.rstrip(".").casefold()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    try:
        ipaddress.ip_address(hostname)
        return None
    except ValueError:
        pass
    if "." not in hostname or hostname in {"facebook.com", "instagram.com"}:
        return None
    try:
        return hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def unavailable_review(
    reason: str,
    *,
    domain: str | None = None,
    refreshed_at: datetime | None = None,
) -> ReviewEnrichmentResult:
    return ReviewEnrichmentResult(
        status="unavailable",
        reason=reason,
        attempted_domain=domain,
        refreshed_at=refreshed_at,
    )


def _parse_business_unit(
    payload: dict[str, object],
    *,
    expected_domain: str,
    minimum_desirable_reviews: int,
    observed_at: datetime,
) -> ReviewStats | None:
    business_unit_id = payload.get("id")
    name = payload.get("name")
    reviews = payload.get("numberOfReviews")
    score = payload.get("score")
    if not isinstance(business_unit_id, str) or not business_unit_id:
        raise ProviderError("Trustpilot response is missing the documented business-unit id")
    if not isinstance(name, dict):
        raise ProviderError("Trustpilot response is missing the documented name object")
    identifying = normalize_domain(str(name.get("identifying", "")))
    referring_raw = name.get("referring")
    referring = set()
    if isinstance(referring_raw, list):
        referring = {
            domain
            for item in referring_raw
            if isinstance(item, str)
            and (domain := normalize_domain(item)) is not None
        }
    if expected_domain != identifying and expected_domain not in referring:
        return None
    if not isinstance(reviews, dict):
        raise ProviderError(
            "Trustpilot response is missing documented numberOfReviews"
        )
    total = reviews.get("total")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ProviderError(
            "Trustpilot response has an invalid numberOfReviews.total"
        )
    trust_score = _optional_score(score, "trustScore")
    stars = _optional_score(score, "stars")
    return ReviewStats(
        source="Trustpilot",
        review_count=total,
        rating=trust_score,
        trust_score=trust_score,
        star_score=stars,
        business_unit_id=business_unit_id,
        matched_domain=expected_domain,
        desirable=total >= minimum_desirable_reviews,
        observed_at=observed_at,
    )


def _optional_score(score: object, field: str) -> float | None:
    if not isinstance(score, dict) or score.get(field) is None:
        return None
    value = score[field]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ProviderError(f"Trustpilot response has an invalid score.{field}")
    normalized = float(value)
    if not 0 <= normalized <= 5:
        raise ProviderError(f"Trustpilot response has an out-of-range score.{field}")
    return normalized


def _parse_apify_business(
    payload: dict[str, object],
    *,
    expected_domain: str,
    minimum_desirable_reviews: int,
    observed_at: datetime,
) -> ReviewStats | None:
    """Normalize only fields documented for the Actor's business output."""

    if payload.get("type") != "business":
        raise ProviderError("Apify Trustpilot result is not a business record")
    business_id = payload.get("businessId")
    if not isinstance(business_id, str) or not business_id:
        raise ProviderError("Apify Trustpilot business result has no businessId")
    result_domain = payload.get("domain")
    if not isinstance(result_domain, str):
        raise ProviderError("Apify Trustpilot business result has no domain")
    normalized_result = normalize_domain(result_domain)
    if normalized_result != expected_domain:
        return None
    reviews = payload.get("numberOfReviews")
    if not isinstance(reviews, int) or isinstance(reviews, bool) or reviews < 0:
        raise ProviderError(
            "Apify Trustpilot business result has invalid numberOfReviews"
        )
    trust_score = _apify_optional_score(payload.get("trustScore"), "trustScore")
    stars = _apify_optional_score(payload.get("stars"), "stars")
    return ReviewStats(
        source="Trustpilot via Apify",
        review_count=reviews,
        rating=trust_score,
        trust_score=trust_score,
        star_score=stars,
        business_unit_id=business_id,
        matched_domain=expected_domain,
        desirable=reviews >= minimum_desirable_reviews,
        observed_at=observed_at,
    )


def _apify_optional_score(value: object, field: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ProviderError(f"Apify Trustpilot business result has invalid {field}")
    normalized = float(value)
    if not 0 <= normalized <= 5:
        raise ProviderError(
            f"Apify Trustpilot business result has out-of-range {field}"
        )
    return normalized


def _apify_response_json(response: httpx.Response, operation: str) -> object:
    if response.status_code in {401, 403}:
        raise ProviderConfigurationError(
            f"{operation} rejected APIFY_API_TOKEN or Actor access"
        )
    if response.status_code >= 400:
        raise ProviderError(f"{operation} failed with HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError(f"{operation} returned malformed JSON") from exc


def _apify_response_object(
    response: httpx.Response, operation: str
) -> dict[str, object]:
    payload = _apify_response_json(response, operation)
    if not isinstance(payload, dict):
        raise ProviderError(f"{operation} returned a non-object response")
    return payload


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return max(0.0, seconds)
