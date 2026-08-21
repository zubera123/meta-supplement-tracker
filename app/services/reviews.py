"""Trustpilot review enrichment using the documented public Business Units API."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from app.models import AdRecord, Brand, ReviewEnrichmentResult, ReviewStats
from app.services import (
    ProviderConfigurationError,
    ProviderError,
    TransientProviderError,
)


logger = logging.getLogger(__name__)
TRUSTPILOT_BASE_URL = "https://api.trustpilot.com"
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})


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


def resolve_advertiser_domain(record: AdRecord) -> tuple[str | None, str]:
    """Resolve one consistent real destination domain without brand-name guessing."""

    candidates: set[str] = set()
    if record.brand.website is not None:
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
        return None, "No genuine advertiser or landing-page domain was returned"
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


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return max(0.0, seconds)
