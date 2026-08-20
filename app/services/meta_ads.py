"""Meta Ad Library provider using Meta's documented Graph API."""

import json
import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.models import AdRecord, Brand, MetaAdDetails, Region
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
        "matched_regions": list(
            dict.fromkeys([*existing.matched_regions, *duplicate.matched_regions])
        )
    }
    for field_name in (
        "eu_total_reach",
        "total_reach_by_location",
        "age_country_gender_reach_breakdown",
        "target_ages",
        "target_gender",
        "target_locations",
        "beneficiary_payers",
    ):
        current_value = getattr(existing, field_name)
        duplicate_value = getattr(duplicate, field_name)
        if current_value in (None, [], "") and duplicate_value not in (None, [], ""):
            updates[field_name] = duplicate_value
    return existing.model_copy(update=updates)
