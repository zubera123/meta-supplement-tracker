"""Orchestration skeleton for a complete supplement-brand scan."""

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import Settings
from app.models import Brand, BrandCandidate, ReviewStats, SocialStats
from app.services import TransientProviderError
from app.services.google_docs import BrandOutputProvider
from app.services.instagram import InstagramProvider
from app.services.meta_ads import MetaAdsProvider
from app.services.reviews import ReviewsProvider
from app.services.scoring import CandidateScorer


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
