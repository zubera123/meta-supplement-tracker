"""Brand scan orchestration and a manual Meta-only discovery command."""

import argparse
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import Settings, get_settings
from app.logging_config import configure_logging
from app.models import AdRecord, Brand, BrandCandidate, ReviewStats, SocialStats
from app.services import ProviderError, TransientProviderError
from app.services.google_docs import BrandOutputProvider
from app.services.instagram import InstagramProvider
from app.services.meta_ads import (
    ApifyMetaAdsProvider,
    MetaAdLibraryProvider,
    MetaAdsProvider,
)
from app.services.reviews import ReviewsProvider
from app.services.scoring import CandidateScorer
from app.services.scoring import instagram_follower_filter


logger = logging.getLogger(__name__)
T = TypeVar("T")


class BrandScanJob:
    """Coordinate retrieval, enrichment, evaluation, and output."""

    def __init__(
        self,
        *,
        settings: Settings,
        meta_ads: MetaAdsProvider,
        instagram: InstagramProvider,
        reviews: ReviewsProvider,
        scorer: CandidateScorer,
        output: BrandOutputProvider,
    ) -> None:
        self.settings = settings
        self.meta_ads = meta_ads
        self.instagram = instagram
        self.reviews = reviews
        self.scorer = scorer
        self.output = output

    async def _with_retry(self, operation: Callable[[], Awaitable[T]]) -> T:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self.settings.provider_retry_attempts),
            wait=wait_exponential(
                min=self.settings.provider_retry_min_wait_seconds,
                max=self.settings.provider_retry_max_wait_seconds,
            ),
            retry=retry_if_exception_type(TransientProviderError),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                return await operation()
        raise RuntimeError("Retry loop ended without a result")

    async def _enrich_social(self, brand: Brand) -> SocialStats | None:
        try:
            return await self._with_retry(lambda: self.instagram.get_social_stats(brand))
        except Exception:
            logger.exception("Instagram enrichment failed", extra={"brand": brand.name})
            return None

    async def _enrich_reviews(self, brand: Brand) -> ReviewStats | None:
        try:
            return await self._with_retry(lambda: self.reviews.get_review_stats(brand))
        except Exception:
            logger.exception("Review enrichment failed", extra={"brand": brand.name})
            return None

    async def run(self) -> Sequence[BrandCandidate]:
        """Run one scan and return every qualifying candidate written to output."""

        logger.info("Starting brand scan")
        ad_records = await self._with_retry(
            lambda: self.meta_ads.retrieve_advertisers(
                regions=self.settings.regions,
                categories=self.settings.categories,
            )
        )
        qualifying: list[BrandCandidate] = []

        for ad_record in ad_records:
            brand = ad_record.brand
            social_stats = ad_record.social_stats
            if social_stats is None:
                social_stats = await self._enrich_social(brand)
            review_stats = await self._enrich_reviews(brand)
            evaluated = self.scorer.evaluate(
                BrandCandidate(
                    brand=brand,
                    ad_record=ad_record,
                    social_stats=social_stats,
                    review_stats=review_stats,
                )
            )
            if evaluated.qualifies:
                qualifying.append(evaluated)

        await self._with_retry(lambda: self.output.write_candidates(qualifying))
        logger.info(
            "Brand scan completed",
            extra={"advertisers": len(ad_records), "qualifying": len(qualifying)},
        )
        return qualifying


async def _run_meta_only(settings: Settings) -> int:
    configured_provider = (settings.meta_ad_provider or "meta_ad_library").casefold()
    if configured_provider == "apify":
        provider: MetaAdsProvider = ApifyMetaAdsProvider(
            api_token=settings.apify_api_token,
            actor_id=settings.apify_actor_id,
            max_results_per_query=settings.apify_max_results_per_query,
            max_total_charge_usd_per_run=(
                settings.apify_max_total_charge_usd_per_run
            ),
            include_advertiser_details=(
                settings.apify_include_advertiser_details
            ),
            monthly_budget_gbp=settings.apify_monthly_budget_gbp,
            budget_gbp_per_usd=settings.apify_budget_gbp_per_usd,
            request_timeout_seconds=settings.apify_request_timeout_seconds,
            retry_attempts=settings.provider_retry_attempts,
            retry_min_wait_seconds=settings.provider_retry_min_wait_seconds,
            retry_max_wait_seconds=settings.provider_retry_max_wait_seconds,
        )
    elif configured_provider == "meta_ad_library":
        provider = MetaAdLibraryProvider(
            access_token=settings.meta_access_token,
            api_version=settings.meta_api_version,
            request_timeout_seconds=settings.meta_request_timeout_seconds,
            max_pages_per_query=settings.meta_max_pages_per_query,
            retry_attempts=settings.provider_retry_attempts,
            retry_min_wait_seconds=settings.provider_retry_min_wait_seconds,
            retry_max_wait_seconds=settings.provider_retry_max_wait_seconds,
        )
    else:
        raise ProviderError(
            f"Unsupported META_AD_PROVIDER for --meta-only: {settings.meta_ad_provider}"
        )
    records = await provider.retrieve_advertisers(
        regions=settings.regions,
        categories=settings.categories,
    )
    print(json.dumps(_build_meta_only_output(records, settings), indent=2))
    return 0


def _build_meta_only_output(
    records: Sequence[AdRecord], settings: Settings
) -> dict[str, object]:
    advertisers: list[dict[str, object]] = []
    for record in records:
        social = record.social_stats
        followers = social.instagram_followers if social else None
        filter_result = instagram_follower_filter(
            followers,
            minimum=settings.target_min_instagram_followers,
            maximum=settings.target_max_instagram_followers,
        )
        advertisers.append(
            {
                "facebook_page_name": record.brand.name,
                "facebook_page_id": record.brand.source_id,
                "active_ad_count": record.active_ad_count,
                "instagram_username": (
                    social.instagram_handle if social else None
                ),
                "instagram_profile_url": (
                    str(social.instagram_profile_url)
                    if social and social.instagram_profile_url
                    else None
                ),
                "instagram_followers": followers,
                "passes_instagram_follower_filter": filter_result,
                "instagram_follower_filter_status": (
                    "unknown"
                    if filter_result is None
                    else "pass" if filter_result else "fail"
                ),
            }
        )
    return {
        "unique_advertisers": len(advertisers),
        "unique_ads": sum(len(record.ads) for record in records),
        "follower_filter": {
            "minimum": settings.target_min_instagram_followers,
            "maximum": settings.target_max_instagram_followers,
        },
        "advertisers": advertisers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Meta Supplement Tracker scan utilities")
    parser.add_argument(
        "--meta-only",
        action="store_true",
        help="Run documented Meta Ad Library discovery without other enrichments",
    )
    args = parser.parse_args()
    if not args.meta_only:
        parser.error("Only --meta-only is available until the remaining providers exist")

    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        return asyncio.run(_run_meta_only(settings))
    except ProviderError as exc:
        logger.error("Meta-only scan failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
