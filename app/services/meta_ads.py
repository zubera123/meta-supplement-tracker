"""Meta advertising providers backed by documented HTTP contracts."""

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal
from urllib.parse import quote
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.models import AdRecord, Brand, MetaAdDetails, Region, SocialStats
from app.services import ProviderConfigurationError, ProviderError, TransientProviderError


logger = logging.getLogger(__name__)

# Meta documents commercial (ad_type=ALL) API access only for ads delivered to
# the UK or EU. "Europe" therefore means the EU-27, not geographic Europe.
EU_COUNTRY_CODES: tuple[str, ...] = (
    "AT",
    "BE",
    "BG",
    "HR",
    "CY",
    "CZ",
    "DK",
    "EE",
    "FI",
    "FR",
    "DE",
    "GR",
    "HU",
    "IE",
    "IT",
    "LV",
    "LT",
    "LU",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SK",
    "SI",
    "ES",
    "SE",
)

COMMERCIAL_REGION_COUNTRIES: dict[str, tuple[Region, tuple[str, ...]]] = {
    "uk": (Region.UK, ("GB",)),
    "europe": (Region.EUROPE, EU_COUNTRY_CODES),
}

UNSUPPORTED_COMMERCIAL_REGIONS: dict[str, str] = {
    "usa": (
        "Meta's official Ad Library API does not provide general commercial-ad "
        "discovery for ads that did not reach the UK or EU"
    ),
    "canada": (
        "Meta's official Ad Library API does not provide general commercial-ad "
        "discovery for ads that did not reach the UK or EU"
    ),
}

# Every requested field is documented on Meta's Archived Ad reference. Spend
# and impressions are intentionally absent because they are political-ad only.
META_AD_FIELDS: tuple[str, ...] = (
    "id",
    "page_id",
    "page_name",
    "ad_creation_time",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "ad_snapshot_url",
    "ad_creative_bodies",
    "ad_creative_link_captions",
    "ad_creative_link_descriptions",
    "ad_creative_link_titles",
    "publisher_platforms",
    "languages",
    "eu_total_reach",
    "total_reach_by_location",
    "age_country_gender_reach_breakdown",
    "target_ages",
    "target_gender",
    "target_locations",
    "beneficiary_payers",
)

_THROTTLE_GRAPH_ERROR_CODES = {4, 17, 32, 613}

# The Actor's current country enum does not contain every EU member state.
APIFY_EU_COUNTRY_CODES: tuple[str, ...] = tuple(
    code
    for code in EU_COUNTRY_CODES
    if code
    in {
        "AT", "BE", "CZ", "DK", "FI", "FR", "DE", "GR", "HU", "IE",
        "IT", "NL", "PL", "PT", "RO", "ES", "SE",
    }
)
APIFY_UNAVAILABLE_EU_COUNTRY_CODES: tuple[str, ...] = tuple(
    code for code in EU_COUNTRY_CODES if code not in APIFY_EU_COUNTRY_CODES
)
APIFY_REGION_COUNTRIES: dict[str, tuple[Region, tuple[str, ...]]] = {
    "uk": (Region.UK, ("GB",)),
    "europe": (Region.EUROPE, APIFY_EU_COUNTRY_CODES),
    "eu": (Region.EUROPE, APIFY_EU_COUNTRY_CODES),
    "usa": (Region.USA, ("US",)),
    "canada": (Region.CANADA, ("CA",)),
}
APIFY_ACTOR_START_USD = Decimal("0.005")
APIFY_AD_RESULT_USD = Decimal("0.0004")
APIFY_CREATIVE_DETAILS_USD = Decimal("0.0001")
APIFY_ADVERTISER_DETAILS_USD = Decimal("0.0004")
_APIFY_TERMINAL_STATUSES = {
    "SUCCEEDED",
    "FAILED",
    "TIMED-OUT",
    "ABORTED",
}


class MetaAdsProvider(ABC):
    """Interface for a real, configured advertiser data source."""

    @abstractmethod
    async def retrieve_advertisers(
        self, *, regions: Sequence[str], categories: Sequence[str]
    ) -> list[AdRecord]:
        """Return normalized advertiser records from the provider."""


class UnconfiguredMetaAdsProvider(MetaAdsProvider):
    async def retrieve_advertisers(
        self, *, regions: Sequence[str], categories: Sequence[str]
    ) -> list[AdRecord]:
        raise ProviderConfigurationError("No Meta ads provider has been configured")


class ApifyMetaAdsProvider(MetaAdsProvider):
    """Discover commercial ads with SolidCode's documented Apify Actor."""

    _api_base = "https://api.apify.com/v2"

    def __init__(
        self,
        *,
        api_token: str | None,
        actor_id: str = "solidcode/meta-ads-library-scraper",
        actor_build: str = "1.0.7",
        max_results_per_query: int = 15,
        max_total_charge_usd_per_run: float = 0.019,
        include_advertiser_details: bool = True,
        monthly_budget_gbp: float = 30.0,
        budget_gbp_per_usd: float = 1.0,
        request_timeout_seconds: float = 120.0,
        retry_attempts: int = 3,
        retry_min_wait_seconds: float = 1.0,
        retry_max_wait_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_token:
            raise ProviderConfigurationError(
                "APIFY_API_TOKEN is required when META_AD_PROVIDER=apify"
            )
        if not actor_id.strip():
            raise ProviderConfigurationError("APIFY_ACTOR_ID must not be empty")
        if not actor_build.strip():
            raise ProviderConfigurationError("APIFY_META_ACTOR_BUILD must not be empty")
        if max_results_per_query < 1:
            raise ProviderConfigurationError(
                "APIFY_MAX_RESULTS_PER_QUERY must be at least 1"
            )
        if max_total_charge_usd_per_run <= 0:
            raise ProviderConfigurationError(
                "APIFY_MAX_TOTAL_CHARGE_USD_PER_RUN must be greater than 0"
            )
        if monthly_budget_gbp <= 0 or budget_gbp_per_usd <= 0:
            raise ProviderConfigurationError(
                "Apify budget and GBP-per-USD conversion must be positive"
            )
        max_charge_usd = Decimal(str(max_total_charge_usd_per_run))
        monthly_budget = Decimal(str(monthly_budget_gbp))
        gbp_per_usd = Decimal(str(budget_gbp_per_usd))
        if max_charge_usd * gbp_per_usd > monthly_budget:
            raise ProviderConfigurationError(
                "APIFY_MAX_TOTAL_CHARGE_USD_PER_RUN exceeds the configured "
                "monthly GBP budget after conversion"
            )
        self._api_token = api_token
        self._headers = {"Authorization": f"Bearer {api_token}"}
        self._actor_id = actor_id.strip()
        self._actor_build = actor_build.strip()
        actor_ref = self._actor_id.replace("/", "~")
        self._actor_ref = quote(actor_ref, safe="~")
        self._max_results = max_results_per_query
        self._max_total_charge_usd_per_run = max_charge_usd
        self._include_advertiser_details = include_advertiser_details
        self._monthly_budget_gbp = monthly_budget
        self._gbp_per_usd = gbp_per_usd
        self._request_timeout_seconds = request_timeout_seconds
        self._retry_attempts = retry_attempts
        self._retry_min_wait_seconds = retry_min_wait_seconds
        self._retry_max_wait_seconds = retry_max_wait_seconds
        self._client = client

    async def retrieve_advertisers(
        self, *, regions: Sequence[str], categories: Sequence[str]
    ) -> list[AdRecord]:
        resolved_regions = self._resolve_regions(regions)
        keywords = self._normalize_keywords(categories)
        if not resolved_regions:
            logger.warning("No Apify Meta-ad regions are supported in this scan")
            return []
        if not keywords:
            logger.warning("No Meta search keywords were configured")
            return []

        country_queries = [
            (region, country)
            for region, countries in resolved_regions
            for country in countries
        ]
        planned_cost_usd = (
            self._max_total_charge_usd_per_run * len(country_queries)
        )
        estimated_run_cost_usd = self.estimated_run_cost_usd
        if self._max_total_charge_usd_per_run < estimated_run_cost_usd:
            logger.warning(
                "Apify per-run charge ceiling is below the documented maximum "
                "cost for the configured result limit; the Actor may stop early",
                extra={
                    "charge_ceiling_usd": float(
                        self._max_total_charge_usd_per_run
                    ),
                    "estimated_run_cost_usd": float(estimated_run_cost_usd),
                },
            )

        if self._client is not None:
            ads = await self._retrieve_with_client(
                self._client,
                country_queries,
                keywords,
                planned_cost_usd,
            )
        else:
            async with httpx.AsyncClient(
                timeout=self._request_timeout_seconds,
                follow_redirects=False,
                headers={"Authorization": f"Bearer {self._api_token}"},
            ) as client:
                ads = await self._retrieve_with_client(
                    client,
                    country_queries,
                    keywords,
                    planned_cost_usd,
                )
        return _aggregate_advertisers(
            ads,
            provider="apify",
            provider_metadata={
                "actor_id": self._actor_id,
                "actor_build": self._actor_build,
                "commercial_spend_available": False,
                "max_total_charge_usd_per_run": float(
                    self._max_total_charge_usd_per_run
                ),
                "advertiser_details_included": self._include_advertiser_details,
                "estimated_run_cost_usd": float(estimated_run_cost_usd),
            },
            include_advertiser_details=self._include_advertiser_details,
        )

    @property
    def estimated_run_cost_usd(self) -> Decimal:
        """Maximum documented event cost for one fully populated Actor run."""

        per_result = APIFY_AD_RESULT_USD + APIFY_CREATIVE_DETAILS_USD
        if self._include_advertiser_details:
            per_result += APIFY_ADVERTISER_DETAILS_USD
        return APIFY_ACTOR_START_USD + Decimal(self._max_results) * per_result

    @staticmethod
    def _normalize_keywords(categories: Sequence[str]) -> list[str]:
        """Deduplicate terms without adding limits absent from the Actor schema."""

        normalized: list[str] = []
        seen: set[str] = set()
        for category in categories:
            keyword = category.strip()
            folded = keyword.casefold()
            if keyword and folded not in seen:
                normalized.append(keyword)
                seen.add(folded)
        return normalized

    def _resolve_regions(
        self, regions: Sequence[str]
    ) -> list[tuple[Region, tuple[str, ...]]]:
        resolved: list[tuple[Region, tuple[str, ...]]] = []
        seen: set[Region] = set()
        for requested in regions:
            mapping = APIFY_REGION_COUNTRIES.get(requested.strip().casefold())
            if mapping is None:
                logger.warning(
                    "Skipping unsupported Apify Meta-ad region %s: not present in the "
                    "provider's configured region map",
                    requested,
                )
                continue
            if mapping[0] not in seen:
                resolved.append(mapping)
                seen.add(mapping[0])
        if any(region is Region.EUROPE for region, _ in resolved):
            logger.warning(
                "Apify Actor country schema lacks these EU country codes; skipping them: %s",
                ",".join(APIFY_UNAVAILABLE_EU_COUNTRY_CODES),
            )
        return resolved

    async def _retrieve_with_client(
        self,
        client: httpx.AsyncClient,
        country_queries: Sequence[tuple[Region, str]],
        keywords: Sequence[str],
        planned_cost_usd: Decimal,
    ) -> dict[str, MetaAdDetails]:
        current_usage_usd = await self._get_monthly_usage_usd(client)
        projected_gbp = (current_usage_usd + planned_cost_usd) * self._gbp_per_usd
        if projected_gbp > self._monthly_budget_gbp:
            raise ProviderError(
                "Apify monthly budget guard aborted the scan: current usage plus "
                f"the planned maximum is GBP {projected_gbp:.2f}, exceeding "
                f"APIFY_MONTHLY_BUDGET_GBP={self._monthly_budget_gbp:.2f}"
            )
        logger.info(
            "Apify budget guard approved scan",
            extra={
                "current_usage_usd": float(current_usage_usd),
                "planned_maximum_usd": float(planned_cost_usd),
                "projected_usage_gbp": float(projected_gbp),
            },
        )

        ads_by_id: dict[str, MetaAdDetails] = {}
        for region, country in country_queries:
            raw_items = await self._run_actor(
                client,
                country=country,
                keywords=keywords,
            )
            for index, item in enumerate(raw_items):
                if not isinstance(item, dict):
                    logger.warning(
                        "Skipping malformed Apify dataset item",
                        extra={"country": country, "item_index": index},
                    )
                    continue
                try:
                    ad = self._normalize_ad(item, region, country)
                except (ProviderError, ValueError) as exc:
                    logger.warning(
                        "Skipping malformed Apify ad result: %s",
                        exc,
                        extra={"country": country, "item_index": index},
                    )
                    continue
                existing = ads_by_id.get(ad.ad_id)
                ads_by_id[ad.ad_id] = (
                    ad if existing is None else _merge_duplicate_ad(existing, ad)
                )
        return ads_by_id

    async def _get_monthly_usage_usd(self, client: httpx.AsyncClient) -> Decimal:
        payload = await self._get_json_with_retry(client, f"{self._api_base}/users/me/limits")
        data = payload.get("data")
        current = data.get("current") if isinstance(data, dict) else None
        value = current.get("monthlyUsageUsd") if isinstance(current, dict) else None
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ProviderError(
                "Apify limits response did not contain current.monthlyUsageUsd; "
                "budget guard fails closed"
            )
        return Decimal(str(value))

    async def _run_actor(
        self,
        client: httpx.AsyncClient,
        *,
        country: str,
        keywords: Sequence[str],
    ) -> list[object]:
        endpoint = f"{self._api_base}/acts/{self._actor_ref}/runs"
        actor_input = {
            "searchTerms": list(keywords),
            "country": country,
            "adActiveStatus": "ACTIVE",
            "adType": "ALL",
            "scrapeAdDetails": True,
            "includeAboutPage": self._include_advertiser_details,
            "onlyTotalCount": False,
            "maxResults": self._max_results,
        }
        params = {
            "timeout": str(max(1, int(self._request_timeout_seconds))),
            "build": self._actor_build,
            "maxTotalChargeUsd": format(
                self._max_total_charge_usd_per_run, "f"
            ),
            "restartOnError": "false",
        }
        run_id: str | None = None
        try:
            response = await client.post(
                endpoint, params=params, json=actor_input, headers=self._headers
            )
        except httpx.RequestError as exc:
            # Starting a paid run is intentionally not retried: a lost response is
            # ambiguous and retrying could create a second billable run.
            raise ProviderError(
                f"Apify Actor start request failed without a safe retry: {type(exc).__name__}"
            ) from exc
        try:
            payload = _response_json_object(response, "Apify Actor start")
            if response.status_code >= 400:
                raise ProviderError(_apify_http_error("Actor start", response, payload))
            data = payload.get("data")
            run_id = data.get("id") if isinstance(data, dict) else None
            if not isinstance(run_id, str) or not run_id:
                raise ProviderError("Apify Actor start response did not contain data.id")

            run = await self._wait_for_run(client, run_id)
            status = run.get("status")
            if status != "SUCCEEDED":
                message = run.get("statusMessage")
                detail = f": {message}" if isinstance(message, str) and message else ""
                raise ProviderError(f"Apify Actor run {run_id} ended with {status}{detail}")
            dataset_id = run.get("defaultDatasetId")
            if not isinstance(dataset_id, str) or not dataset_id:
                raise ProviderError("Successful Apify Actor run has no defaultDatasetId")
            return await self._get_dataset_items(client, dataset_id)
        except asyncio.CancelledError:
            if run_id is not None:
                try:
                    await asyncio.wait_for(
                        self._abort_timed_out_run(client, run_id),
                        timeout=min(10.0, self._request_timeout_seconds),
                    )
                except (TimeoutError, ProviderError):
                    logger.exception(
                        "Could not confirm cancellation of Apify Actor run %s",
                        run_id,
                    )
            raise

    async def _wait_for_run(
        self, client: httpx.AsyncClient, run_id: str
    ) -> dict[str, object]:
        deadline = time.monotonic() + self._request_timeout_seconds
        endpoint = f"{self._api_base}/actor-runs/{quote(run_id, safe='')}"
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await self._abort_timed_out_run(client, run_id)
                raise ProviderError(
                    f"Apify Actor run {run_id} exceeded "
                    f"APIFY_REQUEST_TIMEOUT_SECONDS={self._request_timeout_seconds:g}"
                )
            payload = await self._get_json_with_retry(
                client,
                endpoint,
                params={"waitForFinish": str(max(1, min(30, int(remaining))))},
            )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ProviderError("Apify run response did not contain a data object")
            status = data.get("status")
            if status in _APIFY_TERMINAL_STATUSES:
                return data
            if not isinstance(status, str):
                raise ProviderError("Apify run response did not contain a status")

    async def _abort_timed_out_run(self, client: httpx.AsyncClient, run_id: str) -> None:
        endpoint = f"{self._api_base}/actor-runs/{quote(run_id, safe='')}/abort"
        try:
            await client.post(endpoint, headers=self._headers)
        except httpx.RequestError:
            logger.exception("Failed to abort timed-out Apify Actor run %s", run_id)

    async def _get_dataset_items(
        self, client: httpx.AsyncClient, dataset_id: str
    ) -> list[object]:
        endpoint = f"{self._api_base}/datasets/{quote(dataset_id, safe='')}/items"
        items: list[object] = []
        page_size = min(1000, self._max_results)
        offset = 0
        while len(items) < self._max_results:
            page = await self._get_json_list_with_retry(
                client,
                endpoint,
                params={
                    "clean": "true",
                    "format": "json",
                    "offset": str(offset),
                    "limit": str(min(page_size, self._max_results - len(items))),
                },
            )
            items.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)
        return items[: self._max_results]

    async def _get_json_with_retry(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        *,
        params: dict[str, str] | None = None,
    ) -> dict[str, object]:
        result = await self._retry_get(client, endpoint, params=params, expect_list=False)
        if not isinstance(result, dict):
            raise ProviderError("Apify API returned a non-object JSON response")
        return result

    async def _get_json_list_with_retry(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        *,
        params: dict[str, str],
    ) -> list[object]:
        result = await self._retry_get(client, endpoint, params=params, expect_list=True)
        if not isinstance(result, list):
            raise ProviderError("Apify dataset endpoint returned a non-list JSON response")
        return result

    async def _retry_get(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        *,
        params: dict[str, str] | None,
        expect_list: bool,
    ) -> object:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._retry_attempts),
            wait=wait_exponential(
                min=self._retry_min_wait_seconds,
                max=self._retry_max_wait_seconds,
            ),
            retry=retry_if_exception_type(TransientProviderError),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                try:
                    response = await client.get(
                        endpoint, params=params, headers=self._headers
                    )
                except httpx.RequestError as exc:
                    raise TransientProviderError(
                        f"Apify GET request failed: {type(exc).__name__}"
                    ) from exc
                try:
                    payload = response.json()
                except ValueError as exc:
                    if response.status_code == 429 or response.status_code >= 500:
                        raise TransientProviderError(
                            f"Apify returned transient HTTP {response.status_code}"
                        ) from exc
                    raise ProviderError(
                        f"Apify returned non-JSON HTTP {response.status_code}"
                    ) from exc
                if response.status_code == 429 or response.status_code >= 500:
                    raise TransientProviderError(
                        f"Apify returned transient HTTP {response.status_code}"
                    )
                if response.status_code >= 400:
                    object_payload = payload if isinstance(payload, dict) else {}
                    raise ProviderError(
                        _apify_http_error("GET request", response, object_payload)
                    )
                if expect_list and not isinstance(payload, list):
                    raise ProviderError("Apify dataset response was not a JSON list")
                if not expect_list and not isinstance(payload, dict):
                    raise ProviderError("Apify API response was not a JSON object")
                return payload
        raise RuntimeError("Retry loop ended without an Apify response")

    @staticmethod
    def _normalize_ad(
        raw_ad: dict[str, object], region: Region, country: str
    ) -> MetaAdDetails:
        ad_id = raw_ad.get("adArchiveID")
        page_id = raw_ad.get("pageID")
        page_name = raw_ad.get("pageName")
        if not isinstance(ad_id, str) or not ad_id:
            raise ProviderError("Apify ad is missing documented adArchiveID")
        if not isinstance(page_id, str) or not page_id:
            raise ProviderError(f"Apify ad {ad_id} is missing documented pageID")
        if not isinstance(page_name, str) or not page_name:
            raise ProviderError(f"Apify ad {ad_id} is missing documented pageName")
        creative_bodies = _string_list(raw_ad.get("adCreativeBodies"))
        ad_text = raw_ad.get("adText")
        if isinstance(ad_text, str) and ad_text and ad_text not in creative_bodies:
            creative_bodies.insert(0, ad_text)
        return MetaAdDetails(
            ad_id=ad_id,
            page_id=page_id,
            page_name=page_name,
            ad_creation_time=raw_ad.get("adCreationTime"),
            ad_delivery_start_time=raw_ad.get("startDate"),
            ad_delivery_stop_time=raw_ad.get("endDate"),
            ad_status=(raw_ad.get("adStatus") if isinstance(raw_ad.get("adStatus"), str) else None),
            ad_library_url=_string_or_none(raw_ad.get("adLibraryURL")),
            ad_snapshot_url=_sanitize_snapshot_url(raw_ad.get("adSnapshotUrl")),
            landing_page_url=_string_or_none(raw_ad.get("ctaUrl")),
            landing_page_domain=_string_or_none(raw_ad.get("ctaDomain")),
            cta_headline=_string_or_none(raw_ad.get("ctaHeadline")),
            cta_description=_string_or_none(raw_ad.get("ctaDescription")),
            cta_text=_string_or_none(raw_ad.get("ctaText")),
            cta_type=_string_or_none(raw_ad.get("ctaType")),
            advertiser_page_url=_string_or_none(raw_ad.get("pageURL")),
            page_profile_picture_url=_string_or_none(
                raw_ad.get("pageProfilePictureURL")
            ),
            advertiser_country=_string_or_none(raw_ad.get("pageCountry")),
            facebook_page_category=_string_or_none(raw_ad.get("pageCategory")),
            facebook_page_likes=_documented_nonnegative_int(
                raw_ad.get("pageLikes"), field_name="pageLikes", ad_id=ad_id
            ),
            facebook_page_verified=_documented_bool(
                raw_ad.get("pageVerified"), field_name="pageVerified", ad_id=ad_id
            ),
            facebook_page_about=_string_or_none(raw_ad.get("pageAboutText")),
            instagram_handle=_string_or_none(raw_ad.get("pageInstagramUser")),
            instagram_followers=_documented_nonnegative_int(
                raw_ad.get("pageInstagramFollowers"),
                field_name="pageInstagramFollowers",
                ad_id=ad_id,
            ),
            creative_bodies=creative_bodies,
            platforms=_string_list(raw_ad.get("publisherPlatforms")),
            declared_spend=_string_or_none(raw_ad.get("spend")),
            currency=_string_or_none(raw_ad.get("currency")),
            impressions=raw_ad.get("impressions"),
            reach_estimate=_string_or_none(raw_ad.get("reachEstimate")),
            estimated_audience_size=raw_ad.get("estimatedAudienceSize"),
            regions_reached=raw_ad.get("regionsReached"),
            demographics=raw_ad.get("demographics"),
            source=_string_or_none(raw_ad.get("source")),
            source_query=_string_or_none(raw_ad.get("sourceQuery")),
            matched_countries=[country],
            matched_regions=[region],
        )


class MetaAdLibraryProvider(MetaAdsProvider):
    """Discover active UK/EU commercial ads through `/{version}/ads_archive`."""

    def __init__(
        self,
        *,
        access_token: str | None,
        api_version: str = "v26.0",
        request_timeout_seconds: float = 30.0,
        max_pages_per_query: int = 100,
        retry_attempts: int = 3,
        retry_min_wait_seconds: float = 1.0,
        retry_max_wait_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not access_token:
            raise ProviderConfigurationError(
                "META_ACCESS_TOKEN is required for the Meta Ad Library API"
            )
        if not api_version.startswith("v"):
            raise ProviderConfigurationError("Meta API version must look like v26.0")
        self._access_token = access_token
        self._api_version = api_version
        self._endpoint = f"https://graph.facebook.com/{api_version}/ads_archive"
        self._request_timeout_seconds = request_timeout_seconds
        self._max_pages_per_query = max_pages_per_query
        self._retry_attempts = retry_attempts
        self._retry_min_wait_seconds = retry_min_wait_seconds
        self._retry_max_wait_seconds = retry_max_wait_seconds
        self._client = client

    async def retrieve_advertisers(
        self, *, regions: Sequence[str], categories: Sequence[str]
    ) -> list[AdRecord]:
        supported_regions = self._resolve_regions(regions)
        keywords = self._normalize_keywords(categories)
        if not supported_regions:
            logger.warning("No Meta commercial-ad regions are supported in this scan")
            return []
        if not keywords:
            logger.warning("No Meta search keywords were configured")
            return []

        if self._client is not None:
            ads = await self._retrieve_ads(self._client, supported_regions, keywords)
        else:
            async with httpx.AsyncClient(
                timeout=self._request_timeout_seconds,
                follow_redirects=False,
            ) as client:
                ads = await self._retrieve_ads(client, supported_regions, keywords)
        return self._aggregate_advertisers(ads)

    def _resolve_regions(
        self, regions: Sequence[str]
    ) -> list[tuple[Region, tuple[str, ...]]]:
        resolved: list[tuple[Region, tuple[str, ...]]] = []
        seen: set[Region] = set()
        for requested_region in regions:
            key = requested_region.strip().casefold()
            supported = COMMERCIAL_REGION_COUNTRIES.get(key)
            if supported:
                if supported[0] not in seen:
                    resolved.append(supported)
                    seen.add(supported[0])
                continue
            reason = UNSUPPORTED_COMMERCIAL_REGIONS.get(
                key, "The region is not mapped to a documented commercial Ad Library API area"
            )
            logger.warning(
                "Skipping unsupported Meta commercial-ad region %s: %s",
                requested_region,
                reason,
            )
        return resolved

    @staticmethod
    def _normalize_keywords(categories: Sequence[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for category in categories:
            keyword = category.strip()
            folded = keyword.casefold()
            if keyword and folded not in seen:
                if len(keyword) > 100:
                    raise ProviderConfigurationError(
                        "Meta Ad Library search terms must be 100 characters or fewer"
                    )
                normalized.append(keyword)
                seen.add(folded)
        return normalized

    async def _retrieve_ads(
        self,
        client: httpx.AsyncClient,
        supported_regions: Sequence[tuple[Region, tuple[str, ...]]],
        keywords: Sequence[str],
    ) -> dict[str, MetaAdDetails]:
        ads_by_id: dict[str, MetaAdDetails] = {}
        for region, country_codes in supported_regions:
            for keyword in keywords:
                page_ads = await self._query_keyword(
                    client=client,
                    region=region,
                    country_codes=country_codes,
                    keyword=keyword,
                )
                for ad in page_ads:
                    existing = ads_by_id.get(ad.ad_id)
                    if existing is None:
                        ads_by_id[ad.ad_id] = ad
                        continue
                    ads_by_id[ad.ad_id] = _merge_duplicate_ad(existing, ad)
        return ads_by_id

    async def _query_keyword(
        self,
        *,
        client: httpx.AsyncClient,
        region: Region,
        country_codes: Sequence[str],
        keyword: str,
    ) -> list[MetaAdDetails]:
        base_params: dict[str, str] = {
            "access_token": self._access_token,
            "fields": ",".join(META_AD_FIELDS),
            "search_terms": keyword,
            "search_type": "KEYWORD_EXACT_PHRASE",
            "ad_type": "ALL",
            "ad_active_status": "ACTIVE",
            "ad_reached_countries": json.dumps(list(country_codes)),
        }
        params = base_params
        collected: list[MetaAdDetails] = []

        for page_number in range(1, self._max_pages_per_query + 1):
            payload = await self._request_page(client, params)
            data = payload.get("data")
            if not isinstance(data, list):
                raise ProviderError("Meta Ad Library response did not contain a data list")
            if not data:
                return collected

            for raw_ad in data:
                if not isinstance(raw_ad, dict):
                    raise ProviderError("Meta Ad Library returned a non-object ad")
                collected.append(self._normalize_ad(raw_ad, region))

            paging = payload.get("paging")
            if not isinstance(paging, dict) or not paging.get("next"):
                return collected
            cursors = paging.get("cursors")
            after = cursors.get("after") if isinstance(cursors, dict) else None
            if not isinstance(after, str) or not after:
                raise ProviderError(
                    "Meta Ad Library response provided a next page without an after cursor"
                )
            params = {**base_params, "after": after}
            logger.debug(
                "Fetching Meta Ad Library result page",
                extra={"region": region.value, "keyword": keyword, "page": page_number + 1},
            )

        raise ProviderError(
            "Meta Ad Library pagination exceeded META_MAX_PAGES_PER_QUERY; results are incomplete"
        )

    async def _request_page(
        self, client: httpx.AsyncClient, params: dict[str, str]
    ) -> dict[str, object]:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._retry_attempts),
            wait=wait_exponential(
                min=self._retry_min_wait_seconds,
                max=self._retry_max_wait_seconds,
            ),
            retry=retry_if_exception_type(TransientProviderError),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                return await self._request_page_once(client, params)
        raise RuntimeError("Retry loop ended without a Meta API response")

    async def _request_page_once(
        self, client: httpx.AsyncClient, params: dict[str, str]
    ) -> dict[str, object]:
        try:
            response = await client.get(self._endpoint, params=params)
        except httpx.RequestError as exc:
            raise TransientProviderError(
                f"Meta Ad Library network request failed: {type(exc).__name__}"
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            if response.status_code == 429:
                raise ProviderError(
                    "Meta Ad Library returned HTTP 429; stop requests and retry later"
                ) from exc
            if response.status_code >= 500:
                raise TransientProviderError(
                    f"Meta Ad Library returned transient HTTP {response.status_code}"
                ) from exc
            raise ProviderError(
                f"Meta Ad Library returned non-JSON HTTP {response.status_code}"
            ) from exc

        if not isinstance(payload, dict):
            raise ProviderError("Meta Ad Library returned a non-object JSON response")

        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = str(error.get("message") or "Unknown Meta API error")
            is_transient = bool(error.get("is_transient"))
            if code in _THROTTLE_GRAPH_ERROR_CODES:
                raise ProviderError(
                    f"Meta API rate limit {code}: stop requests and retry later"
                )
            if is_transient:
                raise TransientProviderError(f"Meta API transient error {code}: {message}")
            raise ProviderError(f"Meta API error {code}: {message}")

        if response.status_code == 429:
            raise ProviderError(
                "Meta Ad Library returned HTTP 429; stop requests and retry later"
            )
        if response.status_code >= 500:
            raise TransientProviderError(
                f"Meta Ad Library returned transient HTTP {response.status_code}"
            )
        if response.status_code >= 400:
            raise ProviderError(f"Meta Ad Library returned HTTP {response.status_code}")
        return payload

    @staticmethod
    def _normalize_ad(raw_ad: dict[str, object], region: Region) -> MetaAdDetails:
        ad_id = raw_ad.get("id")
        page_id = raw_ad.get("page_id")
        page_name = raw_ad.get("page_name")
        if not isinstance(ad_id, str) or not ad_id:
            raise ProviderError("Meta Ad Library ad is missing its documented id field")
        if not isinstance(page_id, str) or not page_id:
            raise ProviderError(f"Meta ad {ad_id} is missing its documented page_id field")
        if not isinstance(page_name, str) or not page_name:
            raise ProviderError(f"Meta ad {ad_id} is missing its documented page_name field")

        return MetaAdDetails(
            ad_id=ad_id,
            page_id=page_id,
            page_name=page_name,
            ad_creation_time=raw_ad.get("ad_creation_time"),
            ad_delivery_start_time=raw_ad.get("ad_delivery_start_time"),
            ad_delivery_stop_time=raw_ad.get("ad_delivery_stop_time"),
            ad_snapshot_url=_sanitize_snapshot_url(raw_ad.get("ad_snapshot_url")),
            creative_bodies=_string_list(raw_ad.get("ad_creative_bodies")),
            creative_link_captions=_string_list(
                raw_ad.get("ad_creative_link_captions")
            ),
            creative_link_descriptions=_string_list(
                raw_ad.get("ad_creative_link_descriptions")
            ),
            creative_link_titles=_string_list(raw_ad.get("ad_creative_link_titles")),
            platforms=_string_list(raw_ad.get("publisher_platforms")),
            languages=_string_list(raw_ad.get("languages")),
            eu_total_reach=raw_ad.get("eu_total_reach"),
            total_reach_by_location=raw_ad.get("total_reach_by_location"),
            age_country_gender_reach_breakdown=raw_ad.get(
                "age_country_gender_reach_breakdown"
            ),
            target_ages=_string_list(raw_ad.get("target_ages")),
            target_gender=(
                raw_ad.get("target_gender")
                if isinstance(raw_ad.get("target_gender"), str)
                else None
            ),
            target_locations=raw_ad.get("target_locations"),
            beneficiary_payers=raw_ad.get("beneficiary_payers"),
            matched_regions=[region],
        )

    def _aggregate_advertisers(
        self, ads_by_id: dict[str, MetaAdDetails]
    ) -> list[AdRecord]:
        grouped: dict[str, list[MetaAdDetails]] = defaultdict(list)
        for ad in ads_by_id.values():
            grouped[ad.page_id].append(ad)

        records: list[AdRecord] = []
        for page_id, page_ads in grouped.items():
            page_ads.sort(
                key=lambda ad: (
                    ad.ad_delivery_start_time.timestamp()
                    if ad.ad_delivery_start_time is not None
                    else float("-inf")
                )
            )
            regions = list(
                dict.fromkeys(
                    region for ad in page_ads for region in ad.matched_regions
                )
            )
            start_times = [
                ad.ad_delivery_start_time
                for ad in page_ads
                if ad.ad_delivery_start_time is not None
            ]
            records.append(
                AdRecord(
                    brand=Brand(name=page_ads[0].page_name, source_id=page_id),
                    region=regions[0],
                    regions=regions,
                    estimated_monthly_spend_usd=None,
                    active_ad_count=len(page_ads),
                    oldest_active_ad=min(start_times) if start_times else None,
                    newest_active_ad=max(start_times) if start_times else None,
                    ads=page_ads,
                    provider_metadata={
                        "provider": "meta_ad_library",
                        "api_version": self._api_version,
                        "commercial_spend_available": False,
                    },
                )
            )
        return sorted(records, key=lambda record: record.brand.name.casefold())


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _documented_nonnegative_int(
    value: object, *, field_name: str, ad_id: str
) -> int | None:
    """Accept the Actor's documented integer type; malformed data stays unknown."""

    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    logger.warning(
        "Ignoring malformed Apify %s value for ad %s; expected a non-negative integer",
        field_name,
        ad_id,
    )
    return None


def _documented_bool(
    value: object, *, field_name: str, ad_id: str
) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    logger.warning(
        "Ignoring malformed Apify %s value for ad %s; expected a boolean",
        field_name,
        ad_id,
    )
    return None


def _response_json_object(
    response: httpx.Response, operation: str
) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError(
            f"{operation} returned non-JSON HTTP {response.status_code}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderError(f"{operation} returned a non-object JSON response")
    return payload


def _apify_http_error(
    operation: str, response: httpx.Response, payload: dict[str, object]
) -> str:
    error = payload.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    suffix = f": {message}" if isinstance(message, str) and message else ""
    return f"Apify {operation} returned HTTP {response.status_code}{suffix}"


def _aggregate_advertisers(
    ads_by_id: dict[str, MetaAdDetails],
    *,
    provider: str,
    provider_metadata: dict[str, str | int | float | bool | None],
    include_advertiser_details: bool,
) -> list[AdRecord]:
    grouped: dict[str, list[MetaAdDetails]] = defaultdict(list)
    for ad in ads_by_id.values():
        grouped[ad.page_id].append(ad)

    records: list[AdRecord] = []
    for page_id, page_ads in grouped.items():
        page_ads.sort(
            key=lambda ad: (
                ad.ad_delivery_start_time.timestamp()
                if ad.ad_delivery_start_time is not None
                else float("-inf")
            )
        )
        regions = list(
            dict.fromkeys(region for ad in page_ads for region in ad.matched_regions)
        )
        active_ads = [
            ad
            for ad in page_ads
            if ad.ad_status is None or ad.ad_status.casefold() == "active"
        ]
        start_times = [
            ad.ad_delivery_start_time
            for ad in active_ads
            if ad.ad_delivery_start_time is not None
        ]
        social_stats = (
            _aggregate_social_stats(page_id, page_ads)
            if include_advertiser_details
            else None
        )
        records.append(
            AdRecord(
                brand=Brand(
                    name=page_ads[0].page_name,
                    instagram_handle=(
                        social_stats.instagram_handle if social_stats else None
                    ),
                    source_id=page_id,
                ),
                region=regions[0],
                regions=regions,
                estimated_monthly_spend_usd=None,
                active_ad_count=len(active_ads),
                oldest_active_ad=min(start_times) if start_times else None,
                newest_active_ad=max(start_times) if start_times else None,
                ads=page_ads,
                social_stats=social_stats,
                provider_metadata={"provider": provider, **provider_metadata},
            )
        )
    return sorted(records, key=lambda record: record.brand.name.casefold())


def _aggregate_social_stats(
    page_id: str, page_ads: Sequence[MetaAdDetails]
) -> SocialStats:
    handles = list(
        dict.fromkeys(ad.instagram_handle for ad in page_ads if ad.instagram_handle)
    )
    follower_counts = list(
        dict.fromkeys(
            ad.instagram_followers
            for ad in page_ads
            if ad.instagram_followers is not None
        )
    )
    if len(handles) > 1:
        logger.warning(
            "Conflicting Instagram usernames returned for advertiser page %s; "
            "keeping username unknown",
            page_id,
        )
    if len(follower_counts) > 1:
        logger.warning(
            "Conflicting Instagram follower counts returned for advertiser page %s; "
            "keeping follower count unknown",
            page_id,
        )
    return SocialStats(
        instagram_handle=handles[0] if len(handles) == 1 else None,
        instagram_followers=(
            follower_counts[0] if len(follower_counts) == 1 else None
        ),
        # The Actor currently documents no Instagram profile URL output field.
        instagram_profile_url=None,
    )


def _sanitize_snapshot_url(value: object) -> str | None:
    """Remove access tokens Meta may embed in documented snapshot URLs."""

    if not isinstance(value, str) or not value:
        return None
    parts = urlsplit(value)
    safe_query = urlencode(
        [
            (key, item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
            if key.casefold() != "access_token"
        ]
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, safe_query, parts.fragment))


def _merge_duplicate_ad(existing: MetaAdDetails, duplicate: MetaAdDetails) -> MetaAdDetails:
    """Keep one Library ID while filling fields absent from an earlier region query."""

    updates: dict[str, object] = {
        "matched_countries": list(
            dict.fromkeys([*existing.matched_countries, *duplicate.matched_countries])
        ),
        "matched_regions": list(
            dict.fromkeys([*existing.matched_regions, *duplicate.matched_regions])
        )
    }
    for field_name in (
        "ad_creation_time",
        "ad_delivery_start_time",
        "ad_delivery_stop_time",
        "ad_status",
        "ad_library_url",
        "ad_snapshot_url",
        "landing_page_url",
        "landing_page_domain",
        "cta_headline",
        "cta_description",
        "cta_text",
        "cta_type",
        "advertiser_page_url",
        "page_profile_picture_url",
        "advertiser_country",
        "facebook_page_category",
        "facebook_page_likes",
        "facebook_page_verified",
        "facebook_page_about",
        "instagram_handle",
        "instagram_followers",
        "creative_bodies",
        "creative_link_captions",
        "creative_link_descriptions",
        "creative_link_titles",
        "platforms",
        "languages",
        "eu_total_reach",
        "total_reach_by_location",
        "age_country_gender_reach_breakdown",
        "target_ages",
        "target_gender",
        "target_locations",
        "beneficiary_payers",
        "declared_spend",
        "currency",
        "impressions",
        "reach_estimate",
        "estimated_audience_size",
        "regions_reached",
        "demographics",
        "source",
        "source_query",
    ):
        current_value = getattr(existing, field_name)
        duplicate_value = getattr(duplicate, field_name)
        if current_value in (None, [], "") and duplicate_value not in (None, [], ""):
            updates[field_name] = duplicate_value
    return existing.model_copy(update=updates)
